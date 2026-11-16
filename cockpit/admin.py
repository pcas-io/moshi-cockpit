"""moshi operator API (/api/admin) with the admin token from the keychain.

The admin token can create, reset and revoke every identity, so it is stored with a confirmation
dialog (-T "") and read only on an explicit action, never by polling.
"""

from __future__ import annotations

import json
import re
import urllib.error
import urllib.request
from typing import Any, Callable, Dict, List, Optional, Tuple

from cockpit.keychain import Keychain
from cockpit.config import moshi_base
from cockpit.paths import USER_AGENT
from cockpit.text import clean

ULID_RE = re.compile(r"[0-9A-HJKMNP-TV-Z]{26}")
ADMIN_SERVICE = "moshi-admin"
ADMIN_ACCOUNT = "admin"
Opener = Callable[..., Any]


class AdminError(RuntimeError):
    """Safe to print: never contains the admin token. `reason` is moshi's error code (e.g. name_prefix),
    `status` the HTTP status; both empty/0 for errors that did not come from moshi."""

    def __init__(self, message: str, reason: str = "", status: int = 0) -> None:
        super().__init__(message)
        self.reason = reason
        self.status = status


def has_admin_token(keychain: Keychain) -> bool:
    return keychain.exists(ADMIN_SERVICE, ADMIN_ACCOUNT)


def store_admin_token(keychain: Keychain, token: str) -> None:
    keychain.write(ADMIN_SERVICE, ADMIN_ACCOUNT, token, strict=True)  # always with a confirmation dialog


class AdminApi:
    def __init__(self, token: str, moshi_url: str, opener: Opener = urllib.request.urlopen,
                 base: Optional[str] = None) -> None:
        self._token = token
        self._opener = opener
        self._base = base or moshi_base(moshi_url) + "/api/admin"

    @classmethod
    def from_keychain(cls, keychain: Keychain, moshi_url: str, paths, opener: Opener = urllib.request.urlopen) -> "AdminApi":
        """The admin token only for the origin it was stored for (bot admin-token binds it)."""
        from cockpit.tokens import ADMIN, moshi_token
        return cls(moshi_token(paths, keychain, ADMIN, moshi_url), moshi_url, opener)

    def create_agent(self, name: str) -> Tuple[Dict, str]:
        body = self._request("POST", "/agents", {"name": name})
        token = str(body.get("token") or "")
        if not token:
            raise AdminError("moshi hat kein Token geliefert")
        return dict(body.get("agent") or {}), token

    def revoke_agent(self, name: str) -> Dict:
        """Revokes (not deletes) a moshi identity by its name; moshi keeps it for the audit."""
        from cockpit.registry import NAME_RE
        if not NAME_RE.fullmatch(name or ""):  # goes into the URL path
            raise AdminError("Ungültiger moshi-Name")
        return dict(self._request("POST", f"/agents/{name}/revoke", {}) or {})

    def create_registration_token(self, label: str, name_prefix: str, expires_at: str,
                                  max_per_hour: Optional[int] = None) -> Tuple[Dict, str]:
        """A registration token: creates agents only. Always with prefix and expiry here."""
        payload: Dict[str, Any] = {"label": label, "name_prefix": name_prefix, "expires_at": expires_at}
        if max_per_hour is not None:
            payload["max_per_hour"] = max_per_hour
        body = self._request("POST", "/registration-tokens", payload)
        token = str(body.get("token") or "")
        if not token:
            raise AdminError("moshi hat kein Registrier-Token geliefert")
        return dict(body.get("registration_token") or {}), token

    def registration_tokens(self) -> List[Dict]:
        return list(self._request("GET", "/registration-tokens").get("registration_tokens") or [])

    def revoke_registration_token(self, token_id: str) -> Dict:
        if not ULID_RE.fullmatch(token_id or ""):  # goes into the URL path: nothing but a ULID
            raise AdminError("Ungültige Id eines Registrier-Tokens (26 Zeichen, wie bei register-token list)")
        return dict(self._request("POST", f"/registration-tokens/{token_id}/revoke", {}).get("registration_token") or {})

    def _request(self, method: str, path: str, payload: Optional[Dict] = None) -> Dict:
        return request_json(self._opener, self._base + path, self._token, method, payload, _explain)


def request_json(opener: Opener, url: str, token: str, method: str, payload: Optional[Dict],
                 explain: Callable[[urllib.error.HTTPError, str, str], str]) -> Dict:
    """One JSON call to moshi with a Bearer token. Errors are explained without the token."""
    headers = {"Authorization": f"Bearer {token}", "Accept": "application/json", "User-Agent": USER_AGENT}
    data = None
    if payload is not None:
        data = json.dumps(payload).encode()
        headers["Content-Type"] = "application/json"
    request = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with opener(request, timeout=20) as response:
            return json.loads(response.read().decode("utf-8", errors="replace") or "{}")
    except urllib.error.HTTPError as exc:
        reason, message = error_of(exc)  # the body can be read once
        exc.close()
        raise AdminError(explain(exc, reason, message), reason=reason, status=exc.code) from None
    except urllib.error.URLError as exc:
        raise AdminError(f"moshi nicht erreichbar ({clean(str(exc.reason), '?', limit=120)})") from None
    except ValueError:
        raise AdminError("Antwort von moshi ist kein JSON") from None


def error_of(exc: urllib.error.HTTPError) -> Tuple[str, str]:
    """(error, message) from a moshi error body, sanitized; empty strings when there is none."""
    try:
        body = json.loads(exc.read().decode("utf-8", errors="replace"))
    except (ValueError, AttributeError, OSError):
        return "", ""
    if not isinstance(body, dict):
        return "", ""
    return clean(body.get("error"), "", limit=60), clean(body.get("message"), "", limit=200)


def _explain(exc: urllib.error.HTTPError, reason: str, message: str) -> str:
    if exc.code == 404:
        return "moshi kennt diesen Teil von /api/admin noch nicht (nicht deployt) oder die Id gibt es nicht"
    if exc.code in (401, 403):
        return "Admin-Token abgelehnt (moshi-admin in der Keychain prüfen)"
    if exc.code == 409 and reason == "name_taken":
        return NAME_TAKEN
    return f"moshi antwortet {exc.code}: {reason or message}".rstrip(": ")


NAME_TAKEN = ("Name in moshi schon vergeben (auch in anderer Schreibweise). Bestehende Identität: Token im "
              "Dashboard zurücksetzen und mit --browser eintragen")
