"""SecretStores on a Linux machine: the Secret Service (libsecret, via `secret-tool`) when it answers, otherwise
a private file. Secrets never appear in argv, stdout or error messages.

Neither store implements HardenedStore: there is no per-read approval to harden on Linux.
"""

from __future__ import annotations

import contextlib
import errno
import fcntl
import json
import os
import stat
import subprocess
import tempfile
from pathlib import Path
from secrets import token_urlsafe
from typing import Callable, Dict, Iterator, List, Optional, Sequence

from cockpit.secrets import KeychainError, SecretStore, check_names, validate_token

Runner = Callable[..., "subprocess.CompletedProcess[str]"]
Which = Callable[[str], Optional[str]]

SECRET_TOOL_TIMEOUT = 10  # seconds; a keyring that waits for an unlock prompt nobody sees must not hang us
PROBE_TIMEOUT = 5
ATTR_APP = ("application", "moshi-cockpit")
PROBE_ENTRY = ("moshi-cockpit-probe", "probe")

NO_ANSWER = "The Secret Service did not answer. Unlock your keyring (or log in to a desktop session) and try again."
MARKER_NAME = "secrets-store"  # holds "secret-service" once that store took a token: the choice then sticks
MARKER_TEXT = "secret-service\n"

_FILE_VERSION = 1
_LOCK_NAME = ".secrets.lock"
_MAX_FILE = 1024 * 1024

Entries = Dict[str, Dict[str, str]]


def _shown(path: Path) -> str:
    home = os.environ.get("HOME")
    if home:
        try:
            return "~/" + str(path.relative_to(Path(home)))
        except ValueError:
            pass
    return str(path)


class FileStore:
    """Tokens in one JSON file, mode 0600, owned by the user, never a symlink. Every change rewrites the whole
    file atomically under an exclusive lock, so parallel commands never lose an entry."""

    def __init__(self, path: Path) -> None:
        self._path = path
        self.name = f"file {_shown(path)} (0600)"

    def exists(self, service: str, account: str) -> bool:
        return account in self._load().get(service, {})

    def read(self, service: str, account: str) -> str:
        secret = self._load().get(service, {}).get(account)
        if not secret:
            raise KeychainError(f"Token entry '{service}' (account '{account}') is missing from {self._path}")
        return secret

    def write(self, service: str, account: str, secret: str, strict: bool = False) -> None:
        """Stores or replaces an entry. `strict` is accepted and ignored: the file is 0600 as a whole."""
        validate_token(secret)
        check_names(service, account)
        with self._locked():
            entries = self._load()
            entries.setdefault(service, {})[account] = secret
            self._save(entries)

    def delete(self, service: str, account: str) -> None:
        check_names(service, account)
        if not os.path.lexists(self._path):
            return
        with self._locked():
            entries = self._load()
            if account not in entries.get(service, {}):
                return
            del entries[service][account]
            if not entries[service]:
                del entries[service]
            self._save(entries)

    def permission_problem(self) -> Optional[str]:
        """The sentence `read` would refuse with (symlink, owner, mode), or None. Never reads the content."""
        try:
            fd = self._open()
        except KeychainError as error:
            return str(error)
        if fd is not None:
            os.close(fd)
        return None

    def _open(self) -> Optional[int]:
        """Opens the file for reading without following a symlink and checks it. None if it does not exist."""
        path = self._path
        try:
            fd = os.open(str(path), os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        except FileNotFoundError:
            return None
        except OSError as error:
            if error.errno == errno.ELOOP:
                raise KeychainError(f"{path} is a symlink; replace it with a regular file (chmod 600)") from None
            raise KeychainError(f"Could not read {path} ({error.strerror})") from None
        info = os.fstat(fd)
        problem = None
        if not stat.S_ISREG(info.st_mode):
            problem = f"{path} is not a regular file; move it away and connect again"
        elif info.st_uid != os.getuid():
            problem = f"{path} belongs to another user; move it away and connect again"
        elif info.st_mode & 0o077:
            problem = f"{path} is accessible by other users; fix it with: chmod 600 {path}"
        if problem:
            os.close(fd)
            raise KeychainError(problem)
        return fd

    def _load(self) -> Entries:
        fd = self._open()
        if fd is None:
            return {}
        try:
            with os.fdopen(fd, "rb") as handle:
                raw = handle.read(_MAX_FILE + 1)
        except OSError as error:
            raise KeychainError(f"Could not read {self._path} ({error.strerror})") from None
        invalid = f"{self._path} is not a valid token file; move it away and connect again"
        if len(raw) > _MAX_FILE:
            raise KeychainError(f"{self._path} is larger than 1 MiB; move it away and connect again")
        try:
            data = json.loads(raw.decode("utf-8"))
        except (ValueError, RecursionError):  # also UnicodeDecodeError; the message never carries the content
            data = None
        entries = data.get("entries") if isinstance(data, dict) and data.get("version") == _FILE_VERSION else None
        if not isinstance(entries, dict) or not all(
                isinstance(accounts, dict) and all(isinstance(v, str) for v in accounts.values())
                for accounts in entries.values()):
            raise KeychainError(invalid)
        for accounts in entries.values():
            for secret in accounts.values():
                try:
                    validate_token(secret)
                except KeychainError:
                    raise KeychainError(invalid) from None
        return entries

    def _save(self, entries: Entries) -> None:
        _write_atomic(self._path, json.dumps({"version": _FILE_VERSION, "entries": entries}, indent=2,
                                             sort_keys=True) + "\n")

    @contextlib.contextmanager
    def _locked(self) -> Iterator[None]:
        """One writer at a time, across threads and processes (flock on a private lockfile next to the file)."""
        lock = self._path.parent / _LOCK_NAME
        try:
            _ensure_dir(self._path.parent)
            fd = os.open(str(lock), os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
        except OSError as error:
            raise KeychainError(f"Could not lock {lock} ({error.strerror})") from None
        try:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX)
            except OSError as error:
                raise KeychainError(f"Could not lock {lock} ({error.strerror})") from None
            yield
        finally:
            os.close(fd)  # closing releases the flock


def _ensure_dir(folder: Path) -> None:
    """Creates the folder 0700 when it is new; an existing folder keeps its mode."""
    if not folder.is_dir():
        folder.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.chmod(folder, 0o700)  # mode= is masked by the umask


def _write_atomic(path: Path, text: str) -> None:
    """Temp file (0600) in the same folder, fsync, rename over `path`, fsync the folder."""
    tmp = None
    try:
        fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=f".{path.name}.", suffix=".tmp")
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, str(path))
        tmp = None
        folder = os.open(str(path.parent), os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(folder)
        finally:
            os.close(folder)
    except OSError as error:
        if tmp is not None:
            with contextlib.suppress(OSError):
                os.unlink(tmp)
        raise KeychainError(f"Could not write {path} ({error.strerror})") from None


def _attributes(service: str, account: str) -> List[str]:
    return [ATTR_APP[0], ATTR_APP[1], "service", service, "account", account]


def _run(runner: Runner, args: Sequence[str], stdin: str, timeout: int) -> "subprocess.CompletedProcess[str]":
    return runner(list(args), input=stdin, capture_output=True, text=True, check=False, timeout=timeout)


class SecretToolStore:
    """Tokens in the Secret Service. The secret travels over stdin to `secret-tool store`, never via argv.

    With a `marker` path, the first successful write leaves a non-secret marker there, so later runs pick this
    store again without probing and never fall back to the file."""

    name = "Secret Service (libsecret)"

    def __init__(self, runner: Runner = subprocess.run, marker: Optional[Path] = None) -> None:
        self._runner = runner
        self._marker = marker

    def _call(self, command: str, service: str, account: str, stdin: str = "") -> "subprocess.CompletedProcess[str]":
        args = ["secret-tool", command]
        if command == "store":
            args += ["--label", f"moshi-cockpit {service}"]
        try:
            return _run(self._runner, args + _attributes(service, account), stdin, SECRET_TOOL_TIMEOUT)
        except subprocess.TimeoutExpired:
            raise KeychainError(f"Secret Service entry '{service}' is locked or not answering. {NO_ANSWER}") from None
        except OSError as error:
            raise KeychainError(f"Could not run secret-tool for '{service}' ({error.strerror}). {NO_ANSWER}") from None

    def exists(self, service: str, account: str) -> bool:
        result = self._call("lookup", service, account)
        return result.returncode == 0 and bool((result.stdout or "").strip())

    def read(self, service: str, account: str) -> str:
        result = self._call("lookup", service, account)
        secret = (result.stdout or "").strip() if result.returncode == 0 else ""
        if not secret:
            raise KeychainError(f"Secret Service entry '{service}' (account '{account}') is missing or locked")
        return secret

    def write(self, service: str, account: str, secret: str, strict: bool = False) -> None:
        """Stores or replaces an entry. `strict` is accepted and ignored: the Secret Service has no per-read ACL."""
        validate_token(secret)
        check_names(service, account)
        if self._call("store", service, account, stdin=secret).returncode != 0:
            raise KeychainError(f"Could not write Secret Service entry '{service}'. {NO_ANSWER}")
        if self._marker is not None and not os.path.lexists(self._marker):
            try:
                _ensure_dir(self._marker.parent)
            except OSError as error:
                raise KeychainError(f"Could not write {self._marker} ({error.strerror})") from None
            _write_atomic(self._marker, MARKER_TEXT)

    def delete(self, service: str, account: str) -> None:
        check_names(service, account)
        if self._call("clear", service, account).returncode != 0:
            raise KeychainError(f"Could not delete Secret Service entry '{service}'")


def secret_tool_usable(runner: Runner, which: Which) -> bool:
    """True when `secret-tool` exists and a full round trip on a probe entry (store, lookup equal, clear)
    succeeds within PROBE_TIMEOUT per call. A locked keyring that would prompt on a headless machine is not.
    Once the store succeeded, the probe entry is cleared whatever the lookup did."""
    if not which("secret-tool"):
        return False
    attrs = _attributes(*PROBE_ENTRY)
    value = token_urlsafe(16)
    try:
        stored = _run(runner, ["secret-tool", "store", "--label", f"moshi-cockpit {PROBE_ENTRY[0]}"] + attrs,
                      value, PROBE_TIMEOUT)
    except (subprocess.TimeoutExpired, OSError):
        return False
    if stored.returncode != 0:
        return False
    usable = False
    try:
        found = _run(runner, ["secret-tool", "lookup"] + attrs, "", PROBE_TIMEOUT)
        usable = found.returncode == 0 and (found.stdout or "").strip() == value
    except (subprocess.TimeoutExpired, OSError):
        pass
    finally:
        try:
            usable = _run(runner, ["secret-tool", "clear"] + attrs, "", PROBE_TIMEOUT).returncode == 0 and usable
        except (subprocess.TimeoutExpired, OSError):
            usable = False
    return usable


def select_store(home: Path, runner: Runner, which: Which) -> SecretStore:
    """The first choice sticks, so tokens never split across two stores: an existing token file wins, then the
    Secret Service once it took a token (its marker), without probing either. Only on a fresh machine is the
    Secret Service probed: if it works, it is used, else the file. The file path is Paths.secrets_file."""
    folder = home / ".config" / "moshi-cockpit"
    path = folder / "secrets.json"
    if os.path.lexists(path):  # a symlink counts too: the file store then refuses it instead of switching
        return FileStore(path)
    marker = folder / MARKER_NAME
    if os.path.lexists(marker):
        return SecretToolStore(runner, marker)
    if secret_tool_usable(runner, which):
        return SecretToolStore(runner, marker)
    return FileStore(path)
