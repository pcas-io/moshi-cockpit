"""macOS Keychain access via `security`. Secrets never appear in argv, stdout or error messages."""

from __future__ import annotations

import contextlib
import fcntl
import os
import re
import subprocess
from pathlib import Path
from typing import Callable, Iterator, Optional, Sequence

Runner = Callable[..., "subprocess.CompletedProcess[str]"]

# Tokens from ocellio and moshi are URL-safe. A strict charset keeps the
# `security -i` command line injection-free without shell quoting tricks.
_TOKEN_RE = re.compile(r"^[A-Za-z0-9._~\-]{8,512}$")


class KeychainError(RuntimeError):
    """Raised with a message that names the entry, never its value."""


def _run(runner: Runner, args: Sequence[str], stdin: str = "") -> "subprocess.CompletedProcess[str]":
    return runner(list(args), input=stdin, capture_output=True, text=True, check=False)


def default_lock_dir() -> Path:
    override = os.environ.get("MOSHI_COCKPIT_LOCK_DIR")
    if override:
        return Path(override)
    return Path(os.environ.get("HOME") or str(Path.home())) / ".local" / "state" / "moshi-cockpit" / "locks"


class Keychain:
    def __init__(self, runner: Runner = subprocess.run, lock_dir: Optional[Path] = None) -> None:
        self._runner = runner
        self._lock_dir = lock_dir

    @contextlib.contextmanager
    def _lock(self, service: str, account: str) -> Iterator[None]:
        """One strict rewrite per entry at a time, across processes (flock on a private lockfile)."""
        lock_dir = self._lock_dir or default_lock_dir()
        lock_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.chmod(lock_dir, 0o700)  # mode= applies only on creation and is masked by the umask
        fd = os.open(lock_dir / f"{service}__{account}.lock", os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX)
            yield
        finally:
            os.close(fd)  # closing releases the flock

    def exists(self, service: str, account: str) -> bool:
        """Checks presence by attributes only; `-w` is not passed, so no secret is read."""
        result = _run(self._runner, ["security", "find-generic-password", "-a", account, "-s", service])
        return result.returncode == 0

    def read(self, service: str, account: str) -> str:
        result = _run(self._runner, ["security", "find-generic-password", "-a", account, "-s", service, "-w"])
        if result.returncode != 0:
            raise KeychainError(f"Keychain-Eintrag '{service}' (Account '{account}') fehlt oder ist gesperrt")
        secret = result.stdout.strip()
        if not secret:
            raise KeychainError(f"Keychain-Eintrag '{service}' ist leer")
        return secret

    def write(self, service: str, account: str, secret: str, strict: bool = False) -> None:
        """Stores or replaces an entry. The secret travels over stdin to `security -i`, never via argv.

        strict=True leaves the entry with an empty trusted-app list (-T ""): macOS then asks on every
        read. `add -U` would merge the old list into the new one (Apple's merge_access) instead of
        replacing it, so the entry has to be deleted and added again. A strict write is a small
        transaction, serialized per entry by a lock:

        1. Park the secret strict under `<service>.pending` and check that ACL. If that fails, the
           parked copy goes and nothing else has been touched.
        2. Delete the old entry, add the new one strict (one retry), check its ACL. If adding fails,
           or the new entry is not provably strict (then it is deleted), the parked copy stays.
        3. Only then delete the parked copy.

        Never a plaintext copy, never a fallback to a weaker ACL. A parked copy left next to an
        existing entry (an earlier run stopped between the steps) is reported, not guessed at.
        """
        validate_token(secret)
        _check_names(service, account)
        if not strict:
            self._add(service, account, secret, update=True)
            return
        parked = pending_service(service)
        with self._lock(service, account):
            if self.exists(parked, account):
                if self.exists(service, account):
                    raise KeychainError(stale_parked_message(service, account))
                self.delete(parked, account)  # an older parked copy without an entry: replaced below
            self._add(parked, account, secret, strict=True)
            if not self.is_strict(parked, account):
                self.delete(parked, account)
                raise KeychainError(f"Sicherung '{parked}' ließ sich nicht gehärtet anlegen; "
                                    f"'{service}' ist unverändert")
            self._replace_from_parked(service, account, secret)

    def _replace_from_parked(self, service: str, account: str, secret: str) -> None:
        parked = pending_service(service)
        kept = (f"Das Token ist nicht verloren, es liegt gehärtet unter '{parked}'. Erneut ausführen stellt "
                "es wieder her (moshi-cockpit bot add … --strict-keychain)")
        if self.exists(service, account):
            self.delete(service, account)
        for attempt in (1, 2):
            try:
                self._add(service, account, secret, strict=True)
                break
            except KeychainError:
                if attempt == 2:
                    raise KeychainError(f"Keychain-Eintrag '{service}' konnte nicht neu angelegt werden. {kept}") from None
        if not self.is_strict(service, account):
            self.delete(service, account)
            raise KeychainError(f"Keychain-Eintrag '{service}' ist nicht gehärtet und wurde wieder entfernt. {kept}")
        self.delete(parked, account)

    def has_parked(self, service: str, account: str) -> bool:
        return self.exists(pending_service(service), account)

    def restore_parked(self, service: str, account: str) -> bool:
        """After an interrupted strict rewrite (parked copy, no entry): moves it back. True if it did.

        The parked copy is checked strict first and stays until the entry is back and checked."""
        _check_names(service, account)
        parked = pending_service(service)
        with self._lock(service, account):
            if self.exists(service, account) or not self.exists(parked, account):
                return False
            if not self.is_strict(parked, account):
                raise KeychainError(f"Sicherung '{parked}' ist nicht gehärtet; bitte von Hand prüfen")
            self._replace_from_parked(service, account, self.read(parked, account))
        return True

    def _add(self, service: str, account: str, secret: str, strict: bool = False, update: bool = False) -> None:
        flags = (" -U" if update else "") + f' -a "{account}" -s "{service}"' + (' -T ""' if strict else "")
        result = _run(self._runner, ["security", "-i"], stdin=f'add-generic-password{flags} -w "{secret}"\n')
        # `security -i` exits 0 even when the command inside failed; its stderr says so.
        err = (result.stderr or "").lower()
        if result.returncode != 0 or "error" in err or "returned -" in err or "security:" in err:
            raise KeychainError(f"Konnte Keychain-Eintrag '{service}' nicht schreiben")

    def delete(self, service: str, account: str) -> None:
        _check_names(service, account)
        result = _run(self._runner, ["security", "delete-generic-password", "-a", account, "-s", service])
        if result.returncode != 0:
            raise KeychainError(f"Konnte Keychain-Eintrag '{service}' nicht löschen")

    def is_strict(self, service: str, account: str) -> bool:
        """True when reading the entry's secret has no trusted app: macOS asks every time.

        Reads the ACL from `security dump-keychain -a` (attributes and access lists, no secrets;
        without -d nothing is decrypted, so no dialog).
        """
        result = _run(self._runner, ["security", "dump-keychain", "-a"])
        if result.returncode != 0:
            return False
        return strict_in_dump(result.stdout or "", service, account)


_ENTRY_RE = re.compile(r"^\s*entry \d+:\s*$", re.M)


def strict_in_dump(dump: str, service: str, account: str) -> bool:
    """True only if every item for service/account is provably strict. Fail-closed.

    In each matching item every ACL entry that may decrypt (authorizations with `decrypt` or `any`)
    must list no application (`applications (0):`). `applications: <null>` means any application, a
    count above zero means trusted apps; a missing applications line or no such entry at all is
    unknown and counts as not strict.
    """
    blocks = [b for b in dump.split("keychain: ")[1:]
              if f'"svce"<blob>="{service}"' in b and f'"acct"<blob>="{account}"' in b]
    if not blocks:
        return False
    for block in blocks:
        decrypting = 0
        for entry in _ENTRY_RE.split(block)[1:]:
            lines = [line.strip() for line in entry.splitlines()]
            authorizations = next((line for line in lines if line.startswith("authorizations")), None)
            if authorizations is None:
                return False
            words = authorizations.split(":", 1)[-1].split()
            if "decrypt" not in words and "any" not in words:
                continue
            decrypting += 1
            apps = [line for line in lines if line.startswith("applications")]
            if apps != ["applications (0):"]:
                return False
        if decrypting == 0:
            return False
    return True


def pending_service(service: str) -> str:
    return f"{service}.pending"


def stale_parked_message(service: str, account: str) -> str:
    parked = pending_service(service)
    return (f"Neben '{service}' liegt noch die Sicherung '{parked}' aus einem abgebrochenen Lauf. Welcher Wert "
            f"gilt, ist offen: prüfen und den überzähligen Eintrag löschen, z. B. "
            f"security delete-generic-password -s {parked} -a {account}")


def _check_names(service: str, account: str) -> None:
    for part in (service, account):
        if not re.match(r"^[A-Za-z0-9._\-]{1,64}$", part):
            raise KeychainError("Ungültiger Service- oder Account-Name")


def validate_token(secret: str) -> None:
    if not _TOKEN_RE.match(secret or ""):
        raise KeychainError("Token hat ein unerwartetes Format (erlaubt: A-Z a-z 0-9 . _ ~ -, 8–512 Zeichen)")
