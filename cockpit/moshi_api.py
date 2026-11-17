"""moshi's device and registration API (stdlib only).

Three kinds of bearer, each with one use: the device key (`dk_`) only reads the installation status, the
registration token (`rt_`) only creates, loads and adopts agents, an agent's token only acts as that agent.
Every call refuses redirects and caps the body (cockpit.http). Errors never carry a token, not even one a
hostile server echoes back.
"""

from __future__ import annotations

import json
import re
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Dict, Iterable, Optional, Tuple, Union
from urllib.parse import quote

from cockpit.config import moshi_base
from cockpit.http import DEFAULT_TIMEOUT, HttpFailure, Opener, no_redirect_opener, parse_json, send
from cockpit.paths import USER_AGENT
from cockpit.registry import NAME_RE

PENDING = ("authorization_pending", "slow_down", "expired_token", "access_denied")
REASON_RE = re.compile(r"[a-z0-9_]{1,64}")  # moshi's error codes; anything else becomes http_error
TIMESTAMP_RE = re.compile(r"(\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d)(?:\.(\d{1,9}))?(Z|[+-]\d\d:\d\d)")


class MoshiError(RuntimeError):
    """`reason` is moshi's error code (`name_taken`, `legacy_token`, ...) or one of ours:
    `unreachable` (status 0: no answer at all), `redirect` (status: the 3xx, never followed),
    `bad_response` (an answer that is not usable HTTP or JSON, or misses fields; status 0 when the HTTP
    itself was broken), `http_error` (a 4xx/5xx without a moshi error code, e.g. the auth middleware's
    401 "Unauthorized" for a dead device key or agent token), `invalid_name` (refused before any request).
    Never contains a token."""

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
    registration_expires_at: str  # ISO 8601 as moshi sends it; parse_timestamp reads it


def parse_timestamp(text: str) -> datetime:
    """moshi's ISO timestamps (`2026-11-16T10:00:00.000Z`, also without the fraction or with an offset)
    as an aware datetime. datetime.fromisoformat on Python 3.9 takes neither `Z` nor 1, 2, 4, 5 or more
    than 6 fraction digits. Raises ValueError for anything else."""
    match = TIMESTAMP_RE.fullmatch(text.strip()) if isinstance(text, str) else None
    if not match:
        raise ValueError("not an ISO 8601 timestamp")
    seconds, fraction, zone = match.groups()
    micro = f".{(fraction or '').ljust(6, '0')[:6]}" if fraction else ""
    return datetime.fromisoformat(seconds + micro + ("+00:00" if zone == "Z" else zone)).astimezone(timezone.utc)


class MoshiApi:
    def __init__(self, base: str, opener: Optional[Opener] = None, timeout: float = DEFAULT_TIMEOUT) -> None:
        """`base` is moshi's origin or the MCP URL config stores; the API lives at the origin."""
        self._base = moshi_base(base)
        self._opener = opener or no_redirect_opener()
        self._timeout = timeout

    # -- device flow (no bearer, or the device key on a renewal) ----------------------------------

    def device_authorize(self, label: str, device_key: Optional[str] = None) -> DeviceStart:
        body = self._call("POST", "/api/device/authorize", device_key, {"label": label})
        texts = [body.get(k) for k in ("device_code", "user_code", "verification_uri", "verification_uri_complete")]
        numbers = [body.get(k) for k in ("expires_in", "interval")]
        if not all(isinstance(t, str) and t for t in texts) or not all(_positive_int(n) for n in numbers):
            raise MoshiError("bad_response", 200)
        return DeviceStart(*texts, *numbers)

    def device_token(self, device_code: str) -> Union[DeviceGrant, str]:
        """The grant once the person approved, else one of PENDING (the device flow's poll answers)."""
        try:
            body = self._call("POST", "/api/device/token", None, {"device_code": device_code})
        except MoshiError as exc:
            if exc.status == 400 and exc.reason in PENDING:
                return exc.reason
            raise
        installation = body.get("installation")
        key, token, expires = body.get("device_key"), body.get("registration_token"), body.get("registration_expires_at")
        if (not isinstance(installation, dict) or not _text(installation.get("id"))
                or not _text(installation.get("label")) or not (key is None or _text(key))
                or not _text(token) or not _timestamp(expires)):
            raise MoshiError("bad_response", 200)
        return DeviceGrant(key, installation["id"], installation["label"], token, expires)

    def installation_status(self, device_key: str) -> Dict[str, Any]:
        """`{installation: {id, label}, agents: [...]}`. The only use of the device key."""
        body = self._call("GET", "/api/installation/status", device_key)
        agents = body.get("agents")
        if not isinstance(body.get("installation"), dict) or not isinstance(agents, list) or not all(
                isinstance(agent, dict) for agent in agents):
            raise MoshiError("bad_response", 200)
        return body

    # -- agents, with the installation's registration token ---------------------------------------

    def register_agent(self, registration_token: str, name: str, harness: str, role: Optional[str],
                       skill: Optional[str]) -> Tuple[Dict[str, Any], str]:
        """A new local agent, held by this installation; its installation token."""
        payload = _without_none({"name": name, "harness": harness, "role": role, "skill": skill})
        return _agent_and_token(self._call("POST", "/api/register/agents", registration_token, payload))

    def load_agent(self, registration_token: str, name: str) -> Tuple[Dict[str, Any], str]:
        """A fresh token of an existing local agent for this installation (the active one stays)."""
        if not NAME_RE.fullmatch(name or ""):  # goes into the URL path
            raise MoshiError("invalid_name", 0)
        path = f"/api/register/agents/{quote(name, safe='')}/load"
        return _agent_and_token(self._call("POST", path, registration_token, {}))

    def adopt(self, registration_token: str, legacy_token: str, harness: str) -> Dict[str, Any]:
        """Binds an agent's older, installation-less token to this installation, which then holds it."""
        body = self._call("POST", "/api/register/agents/adopt", registration_token,
                          {"token": legacy_token, "harness": harness}, secrets=(legacy_token,))
        agent = body.get("agent")
        if not isinstance(agent, dict):
            raise MoshiError("bad_response", 200)
        return dict(agent)

    # -- the agent's own token --------------------------------------------------------------------

    def activate(self, agent_token: str) -> Optional[str]:
        """Makes this installation the agent's active one. Returns the label of the installation that held
        it before, or None when none did. moshi also returns a label when that was this installation
        already: callers compare it with their own label before they speak of a takeover."""
        previous = self._call("POST", "/api/agent/activate", agent_token).get("previous")
        if previous is not None and not isinstance(previous, str):
            raise MoshiError("bad_response", 200)
        return previous

    def update_profile(self, agent_token: str, role: Optional[str], skill: Optional[str]) -> None:
        """None leaves a field as it is; "" clears it (sent as null: moshi refuses an empty skill name)."""
        changes = _without_none({"role": role, "skill": skill})
        self._call("PATCH", "/api/agent/profile", agent_token, {key: value or None for key, value in changes.items()})

    def drop_token(self, agent_token: str) -> None:
        """Ends the agent's token on this installation; the agent is then held by none if it was here."""
        self._call("DELETE", "/api/agent/token", agent_token)

    def _call(self, method: str, path: str, bearer: Optional[str], payload: Optional[Dict[str, Any]] = None,
              secrets: Iterable[str] = ()) -> Dict[str, Any]:
        return request_json(self._opener, self._base + path, bearer, method, payload,
                            timeout=self._timeout, secrets=tuple(secrets))


def request_json(opener: Opener, url: str, bearer: Optional[str], method: str, payload: Optional[Dict[str, Any]],
                 timeout: float = DEFAULT_TIMEOUT, secrets: Tuple[str, ...] = ()) -> Dict[str, Any]:
    """One JSON call to moshi. `opener` must refuse redirects (cockpit.http.no_redirect_opener).
    `secrets` are other tokens in the payload; like the bearer they never reach the error."""
    headers = {"Accept": "application/json", "User-Agent": USER_AGENT}
    if bearer:
        headers["Authorization"] = f"Bearer {bearer}"
    data = None
    if payload is not None:
        data = json.dumps(payload).encode()
        headers["Content-Type"] = "application/json"
    request = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        reply = send(opener, request, timeout)
        if not reply.body.strip():
            return {}  # 204 and empty bodies
        body = parse_json(reply.body, reply.status)
    except HttpFailure as failure:
        if failure.kind == "http":
            raise MoshiError(_reason(failure, tuple(s for s in (bearer, *secrets) if s)), failure.status) from None
        raise MoshiError(failure.kind, failure.status) from None
    if not isinstance(body, dict):
        raise MoshiError("bad_response", reply.status)
    return body


def _reason(failure: HttpFailure, hidden: Tuple[str, ...]) -> str:
    """moshi's `error` code when it is one; a hostile body that echoes a token gives http_error."""
    try:
        body = parse_json(failure.body, failure.status)
    except HttpFailure:
        return "http_error"
    reason = body.get("error") if isinstance(body, dict) else None
    if not isinstance(reason, str) or not REASON_RE.fullmatch(reason):
        return "http_error"
    if any(secret.lower() in reason or reason in secret.lower() for secret in hidden):
        return "http_error"
    return reason


def _agent_and_token(body: Dict[str, Any]) -> Tuple[Dict[str, Any], str]:
    agent, token = body.get("agent"), body.get("token")
    if not isinstance(agent, dict) or not _text(token):
        raise MoshiError("bad_response", 200)
    return dict(agent), token


def _text(value: Any) -> bool:
    return isinstance(value, str) and bool(value)


def _positive_int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value > 0


def _timestamp(value: Any) -> bool:
    try:
        parse_timestamp(value)
    except ValueError:
        return False
    return True


def _without_none(values: Dict[str, Optional[str]]) -> Dict[str, str]:
    return {key: value for key, value in values.items() if value is not None}
