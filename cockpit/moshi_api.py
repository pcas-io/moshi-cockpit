"""moshi's device and registration API (stdlib only).

Three kinds of bearer, each with one use: the device key (`dk_`) only reads the installation status, the
registration token (`rt_`) only creates, loads and adopts agents, an agent's token only acts as that agent.
Every call refuses redirects: urllib would forward the Authorization header to whatever origin a 3xx names.
Errors never carry a token, not even one a hostile server echoes back.
"""

from __future__ import annotations

import json
import re
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Any, Callable, Dict, Iterable, Optional, Tuple, Union
from urllib.parse import quote

from cockpit.config import moshi_base
from cockpit.paths import USER_AGENT
from cockpit.registry import NAME_RE

Opener = Callable[..., Any]
DEFAULT_TIMEOUT = 10.0
PENDING = ("authorization_pending", "slow_down", "expired_token", "access_denied")
REASON_RE = re.compile(r"[a-z0-9_]{1,64}")  # moshi's error codes; anything else becomes http_error


class _RefuseRedirects(urllib.request.HTTPRedirectHandler):
    """Turns every 3xx into an HTTPError instead of a second request."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: D102 (urllib API)
        raise urllib.error.HTTPError(req.full_url, code, msg, headers, fp)


def no_redirect_opener() -> Opener:
    """`urlopen` without redirects: the opener for every request that carries a bearer token."""
    return urllib.request.build_opener(_RefuseRedirects()).open


class MoshiError(RuntimeError):
    """`reason` is moshi's error code (`name_taken`, `legacy_token`, ...) or one of ours: `unreachable`
    (status 0), `redirect`, `bad_response`, `http_error`, `invalid_name`. Never contains a token."""

    def __init__(self, reason: str, status: int) -> None:
        self.reason = reason
        self.status = status
        super().__init__(f"moshi: {reason} (HTTP {status})" if status else f"moshi: {reason}")


@dataclass(frozen=True)
class DeviceStart:
    device_code: str
    user_code: str
    verification_uri: str
    verification_uri_complete: str
    expires_in: int
    interval: int


@dataclass(frozen=True)
class DeviceGrant:
    device_key: Optional[str]  # None on a renewal: the installation keeps its key
    installation_id: str
    installation_label: str
    registration_token: str
    registration_expires_at: str


class MoshiApi:
    def __init__(self, base: str, opener: Optional[Opener] = None, timeout: float = DEFAULT_TIMEOUT) -> None:
        """`base` is moshi's origin or the MCP URL config stores (`https://host/mcp`)."""
        self._base = moshi_base(base)
        self._opener = opener or no_redirect_opener()
        self._timeout = timeout

    # -- device flow (no bearer, or the device key on a renewal) ----------------------------------

    def device_authorize(self, label: str, device_key: Optional[str] = None) -> DeviceStart:
        body = self._call("POST", "/api/device/authorize", device_key, {"label": label})
        try:
            return DeviceStart(str(body["device_code"]), str(body["user_code"]), str(body["verification_uri"]),
                               str(body["verification_uri_complete"]), int(body["expires_in"]),
                               int(body["interval"]))
        except (KeyError, TypeError, ValueError):
            raise MoshiError("bad_response", 200) from None

    def device_token(self, device_code: str) -> Union[DeviceGrant, str]:
        """The grant once the person approved, else one of PENDING (the device flow's poll answers)."""
        try:
            body = self._call("POST", "/api/device/token", None, {"device_code": device_code})
        except MoshiError as exc:
            if exc.status == 400 and exc.reason in PENDING:
                return exc.reason
            raise
        installation = body.get("installation")
        key = body.get("device_key")
        if not isinstance(installation, dict) or not body.get("registration_token") or not (
                key is None or isinstance(key, str)):
            raise MoshiError("bad_response", 200)
        return DeviceGrant(key or None, str(installation.get("id") or ""), str(installation.get("label") or ""),
                           str(body["registration_token"]), str(body.get("registration_expires_at") or ""))

    def installation_status(self, device_key: str) -> Dict[str, Any]:
        """`{installation: {id, label}, agents: [...]}`. The only use of the device key."""
        return self._call("GET", "/api/installation/status", device_key)

    # -- agents, with the installation's registration token ---------------------------------------

    def register_agent(self, registration_token: str, name: str, harness: str, role: Optional[str],
                       skill: Optional[str]) -> Tuple[Dict[str, Any], str]:
        payload = _without_none({"name": name, "harness": harness, "role": role, "skill": skill})
        return _agent_and_token(self._call("POST", "/api/register/agents", registration_token, payload))

    def load_agent(self, registration_token: str, name: str) -> Tuple[Dict[str, Any], str]:
        """A fresh token of an existing agent for this installation."""
        if not NAME_RE.fullmatch(name or ""):  # goes into the URL path
            raise MoshiError("invalid_name", 0)
        path = f"/api/register/agents/{quote(name, safe='')}/load"
        return _agent_and_token(self._call("POST", path, registration_token, {}))

    def adopt(self, registration_token: str, legacy_token: str, harness: str) -> Dict[str, Any]:
        """Binds an agent's older, installation-less token to this installation."""
        body = self._call("POST", "/api/register/agents/adopt", registration_token,
                          {"token": legacy_token, "harness": harness}, secrets=(legacy_token,))
        return dict(body.get("agent") or {})

    # -- the agent's own token --------------------------------------------------------------------

    def activate(self, agent_token: str) -> Optional[str]:
        """Makes this installation the agent's active one; returns the label it was active on before."""
        previous = self._call("POST", "/api/agent/activate", agent_token).get("previous")
        return str(previous) if previous else None

    def update_profile(self, agent_token: str, role: Optional[str], skill: Optional[str]) -> None:
        self._call("PATCH", "/api/agent/profile", agent_token, _without_none({"role": role, "skill": skill}))

    def drop_token(self, agent_token: str) -> None:
        self._call("DELETE", "/api/agent/token", agent_token)

    def _call(self, method: str, path: str, bearer: Optional[str], payload: Optional[Dict[str, Any]] = None,
              secrets: Iterable[str] = ()) -> Dict[str, Any]:
        return request_json(self._opener, self._base + path, bearer, method, payload,
                            timeout=self._timeout, secrets=tuple(secrets))


def request_json(opener: Opener, url: str, bearer: Optional[str], method: str, payload: Optional[Dict[str, Any]],
                 timeout: float = DEFAULT_TIMEOUT, secrets: Tuple[str, ...] = ()) -> Dict[str, Any]:
    """One JSON call to moshi. `opener` must refuse redirects when a bearer is sent (see no_redirect_opener).
    `secrets` are other tokens in the payload; like the bearer they never reach the error."""
    headers = {"Accept": "application/json", "User-Agent": USER_AGENT}
    if bearer:
        headers["Authorization"] = f"Bearer {bearer}"
    data = None
    if payload is not None:
        data = json.dumps(payload).encode()
        headers["Content-Type"] = "application/json"
    hidden = tuple(s for s in (bearer, *secrets) if s)
    request = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with opener(request, timeout=timeout) as response:
            status = int(getattr(response, "status", 200) or 200)
            if 300 <= status < 400:  # an opener that hands a 3xx back instead of raising
                raise MoshiError("redirect", status)
            return _json_object(response.read(), status)
    except urllib.error.HTTPError as exc:
        reason = "redirect" if 300 <= exc.code < 400 else _reason(exc, hidden)
        exc.close()  # the error holds the open response
        raise MoshiError(reason, exc.code) from None
    except (urllib.error.URLError, OSError):  # refused, DNS, TLS, timeout
        raise MoshiError("unreachable", 0) from None


def _json_object(raw: bytes, status: int) -> Dict[str, Any]:
    text = raw.decode("utf-8", errors="replace")
    if not text.strip():
        return {}  # 204 and empty bodies
    try:
        body = json.loads(text)
    except ValueError:
        raise MoshiError("bad_response", status) from None
    if not isinstance(body, dict):
        raise MoshiError("bad_response", status)
    return body


def _reason(exc: urllib.error.HTTPError, hidden: Tuple[str, ...]) -> str:
    """moshi's `error` code when it is one; a hostile body that echoes a token gives http_error."""
    try:
        body = json.loads(exc.read().decode("utf-8", errors="replace"))
    except (ValueError, AttributeError, OSError):
        return "http_error"
    reason = body.get("error") if isinstance(body, dict) else None
    if not isinstance(reason, str) or not REASON_RE.fullmatch(reason):
        return "http_error"
    if any(secret.lower() in reason or reason in secret.lower() for secret in hidden):
        return "http_error"
    return reason


def _agent_and_token(body: Dict[str, Any]) -> Tuple[Dict[str, Any], str]:
    agent, token = body.get("agent"), body.get("token")
    if not isinstance(agent, dict) or not isinstance(token, str) or not token:
        raise MoshiError("bad_response", 200)
    return dict(agent), token


def _without_none(values: Dict[str, Optional[str]]) -> Dict[str, str]:
    return {key: value for key, value in values.items() if value is not None}
