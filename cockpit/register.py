"""Registration tokens: agents register without the admin token.

A registration token (`rt_…`) can only create agents, at one route (POST /api/register/agents),
with names that start with its prefix and only until it expires. moshi-cockpit handles it like the
admin token: stored with a confirmation dialog (-T ""), bound to the moshi origin it was stored for,
read only on an explicit action. A refused registration token is an error, never a reason to fall
back to the admin token.

Issuing one needs the admin token and happens on an admin machine: always with the prefix
`<name>-` and an expiry. Its plaintext goes to the clipboard through stdin, never to stdout or argv.
"""

from __future__ import annotations

import re
import shutil
import subprocess
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone
from typing import Callable, Dict, List, Optional, Tuple

from cockpit.admin import NAME_TAKEN, AdminApi, AdminError, Opener, request_json
from cockpit.config import moshi_base
from cockpit.secrets import KeychainError, SecretStore
from cockpit.text import clean

REGISTER_SERVICE = "moshi-register"
REGISTER_ACCOUNT = "register"
# moshi's format: rt_<ulid, lower case>_<32 random bytes, base64url>
TOKEN_RE = re.compile(r"rt_[0-9a-z]{26}_[A-Za-z0-9_-]{43}")
DEFAULT_DAYS = 7
MAX_DAYS = 30
MAX_PREFIX = 41  # moshi's limit for name_prefix
Runner = Callable[..., "subprocess.CompletedProcess[str]"]


def validate_register_token(token: str) -> None:
    if not TOKEN_RE.fullmatch(token or ""):
        raise KeychainError("Das ist kein Registrier-Token (Format rt_…). Agent- und Admin-Tokens gehören nicht hierher")


def has_register_token(keychain: SecretStore) -> bool:
    return keychain.exists(REGISTER_SERVICE, REGISTER_ACCOUNT)


def store_register_token(keychain: SecretStore, token: str) -> None:
    validate_register_token(token)
    keychain.write(REGISTER_SERVICE, REGISTER_ACCOUNT, token, strict=True)  # always with a dialog


def forget_register_token(keychain: SecretStore, paths, backup) -> List[str]:
    """Removes the local registration token and its binding. Needed after a revoke: while one is stored,
    `bot add` uses nothing else (no fallback), so a dead one would block every later `bot add`."""
    from cockpit.tokens import REGISTER, unbind_identity
    notes = []
    if keychain.exists(REGISTER_SERVICE, REGISTER_ACCOUNT):
        keychain.delete(REGISTER_SERVICE, REGISTER_ACCOUNT)
        notes.append(f"{REGISTER_SERVICE}: aus der Keychain entfernt")
    if unbind_identity(paths, backup, REGISTER):
        notes.append("Bindung des Registrier-Tokens entfernt")
    return notes or ["kein Registrier-Token hinterlegt, nichts zu tun"]


class RegisterApi:
    """POST /api/register/agents with the registration token."""

    def __init__(self, token: str, moshi_url: str, opener: Opener = urllib.request.urlopen,
                 base: Optional[str] = None) -> None:
        self._token = token
        self._opener = opener
        self._base = base or moshi_base(moshi_url) + "/api/register"

    @classmethod
    def from_keychain(cls, keychain: SecretStore, moshi_url: str, paths,
                      opener: Opener = urllib.request.urlopen) -> "RegisterApi":
        """The registration token only for the origin it was stored for (bot register-token set binds it)."""
        from cockpit.tokens import REGISTER, moshi_token
        return cls(moshi_token(paths, keychain, REGISTER, moshi_url), moshi_url, opener)

    def create_agent(self, name: str) -> Tuple[Dict, str]:
        body = request_json(self._opener, self._base + "/agents", self._token, "POST", {"name": name}, _explain)
        token = str(body.get("token") or "")
        if not token:
            raise AdminError("moshi hat kein Token geliefert")
        return dict(body.get("agent") or {}), token


def _explain(exc: urllib.error.HTTPError, reason: str, message: str) -> str:
    if exc.code == 404:
        return "moshi kennt /api/register noch nicht (auf diesem Server nicht deployt)"
    if exc.code == 401:
        return ("Registrier-Token abgelehnt: unbekannt, widerrufen oder abgelaufen. Ein neues vom Admin ersetzt es "
                "(moshi-cockpit bot register-token set), oder hier entfernen: moshi-cockpit bot register-token remove")
    if exc.code == 403 and reason == "name_prefix":
        return f"Name nicht erlaubt: {message or 'außerhalb des Präfixes dieses Registrier-Tokens'}"
    if exc.code == 409 and reason == "name_taken":
        return NAME_TAKEN
    if exc.code == 429:
        wait = clean(exc.headers.get("Retry-After") if exc.headers else "", "", limit=10)
        why = "moshi prüft gerade viele Tokens" if reason == "busy" else "zu viele Versuche oder Anlagen"
        return f"{why}, später erneut versuchen" + (f" (in {wait} s)" if wait.isdigit() else "")
    return f"moshi antwortet {exc.code}: {reason or message}".rstrip(": ")


# --- Issuing (admin machine) ---


# moshi's own rule for agent names; the prefix leaves room for "-<agent>".
_PREFIX_RE = re.compile(r"[a-z0-9][a-z0-9_-]{0,40}")


def prefix_for(person: str) -> str:
    name = (person or "").strip().lower()
    if not _PREFIX_RE.fullmatch(name):
        raise ValueError("Invalid name for a registration token prefix (a-z, 0-9, _ and -, at most 41 characters)")
    prefix = f"{name}-"
    if len(prefix) > MAX_PREFIX:
        raise ValueError(f"Name too long for a registration token (at most {MAX_PREFIX - 1} characters)")
    return prefix


def expiry(days: int, now: Optional[datetime] = None) -> str:
    if not 1 <= days <= MAX_DAYS:
        raise ValueError(f"--days: 1 bis {MAX_DAYS}")
    moment = (now or datetime.now(timezone.utc)) + timedelta(days=days)
    return moment.replace(microsecond=0).isoformat().replace("+00:00", "Z")


def clipboard_ready() -> bool:
    return shutil.which("pbcopy") is not None


def copy_to_clipboard(secret: str, runner: Runner = subprocess.run) -> None:
    """The secret goes through stdin; nothing of it in argv, stdout or the error."""
    result = runner(["pbcopy"], input=secret, capture_output=True, text=True, check=False)
    if result.returncode != 0:
        raise KeychainError("Zwischenablage nicht beschreibbar (pbcopy)")


def read_clipboard(runner: Optional[Runner] = None) -> str:
    """For `register-token set --clipboard`: the token never passes a terminal, so nothing typed or
    copied in between can replace it unnoticed. Only a well-formed registration token is accepted."""
    result = (runner or subprocess.run)(["pbpaste"], capture_output=True, text=True, check=False)
    if result.returncode != 0:
        raise KeychainError("Zwischenablage nicht lesbar (pbpaste)")
    token = (result.stdout or "").strip()
    if not TOKEN_RE.fullmatch(token):
        raise KeychainError("In der Zwischenablage liegt kein Registrier-Token (rt_…). Es wird nichts angezeigt; "
                            "das Token neu kopieren und noch einmal versuchen")
    return token


def clear_clipboard(token: str, runner: Optional[Runner] = None) -> bool:
    """Empties the clipboard if it still holds `token` (something copied since stays). True if emptied."""
    run = runner or subprocess.run
    current = run(["pbpaste"], capture_output=True, text=True, check=False)
    if current.returncode != 0 or (current.stdout or "").strip() != token:
        return False
    return run(["pbcopy"], input="", capture_output=True, text=True, check=False).returncode == 0


def issue(api: AdminApi, person: str, days: int = DEFAULT_DAYS, max_per_hour: Optional[int] = None,
          label: Optional[str] = None, copy: Callable[[str], None] = copy_to_clipboard) -> Dict:
    """Creates a registration token for `person` and puts it into the clipboard. If that fails, the
    token is revoked at once: a token nobody holds must not stay valid."""
    prefix = prefix_for(person)
    created, token = api.create_registration_token(clean(label, "", limit=80) or f"moshi-cockpit {prefix[:-1]}",
                                                   prefix, expiry(days), max_per_hour)
    try:
        copy(token)
    except Exception:
        token_id = clean(created.get("id"), "", limit=40)
        try:
            if not token_id:
                raise AdminError("moshi hat keine Id geliefert")
            api.revoke_registration_token(token_id)
        except Exception:
            raise KeychainError("Registrier-Token nicht in die Zwischenablage gebracht und NICHT widerrufen"
                                + (f" (Id {token_id}): moshi-cockpit bot register-token revoke {token_id}" if token_id
                                   else ": in moshi-cockpit bot register-token list suchen und widerrufen")) from None
        raise KeychainError("Registrier-Token nicht in die Zwischenablage gebracht; es wurde sofort widerrufen") from None
    return created


def render_tokens(tokens: List[Dict]) -> str:
    lines = []
    for t in tokens:
        state = "widerrufen" if t.get("revoked_at") else "aktiv"
        lines.append(f"{clean(t.get('id'), '?'):<28}{clean(t.get('name_prefix'), '-'):<16}"
                     f"bis {clean(t.get('expires_at'), 'unbegrenzt')[:16].replace('T', ' '):<18}{state:<12}"
                     f"{int(t.get('created_agents') or 0)} Agent(s)  {clean(t.get('label'), '')}")
    lines.append(f"{len(tokens)} Registrier-Token(s)")
    return "\n".join(lines)
