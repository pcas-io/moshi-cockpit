"""An in-memory moshi 1.4.0 for tests: device flow, installation status, registration, the agent's own API.

Start one with `serve(test_case)`; it returns the `MoshiState` the handler works on. Tests seed and drive
that state directly (`approve`, `add_agent`, `revoke_installation`, ...) and read `requests`, where every
request is recorded with its Authorization header, so a test can prove which token went where.

The answers follow moshi's own routes and services (status codes, error codes, null for unset fields,
token shapes, side effects). Two deliberate differences, both test drivers: the device flow's interval is
only enforced when `enforce_interval` is set (tests do not wait 5 s), and `slow_down` forces that answer.
"""

from __future__ import annotations

import json
import re
import secrets
import string
import threading
import time
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import unquote

HARNESSES = ("claude", "codex", "vibe", "agy", "grok")
NAME_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,63}")  # moshi's AGENT_NAME_RE, and SKILL_RE
RESERVED_NAMES = ("admin", "broadcast")
RT_RE = re.compile(r"rt_([0-9a-z]{26})_([A-Za-z0-9_-]{43})")
ROLE_MAX = 64
LABEL_MAX = 64
DEVICE_EXPIRES_IN = 600
DEVICE_INTERVAL = 5
AUTHORIZE_LIMIT = 10      # per client address per 10 minutes; every test client is 127.0.0.1
REGISTER_PER_HOUR = 10    # a registration token's default max_per_hour
UNSAFE_LABEL = re.compile("[\u0000-\u001f\u007f-\u009f​-‏‪-‮⁦-⁩﻿]")
CONTROL = re.compile("[\u0000-\u001f\u007f]")
Answer = Tuple[int, Optional[Dict[str, Any]]]

ULID_ALPHABET = "0123456789abcdefghjkmnpqrstvwxyz"
URLSAFE = string.ascii_letters + string.digits + "-_"


def _ulid() -> str:
    return "".join(secrets.choice(ULID_ALPHABET) for _ in range(26))


def _iso(moment: datetime) -> str:
    """JavaScript's toISOString(): milliseconds and Z."""
    return moment.strftime("%Y-%m-%dT%H:%M:%S.") + f"{moment.microsecond // 1000:03d}Z"


def _now() -> datetime:
    return datetime.now(timezone.utc)


def clean_label(raw: str) -> str:
    """moshi's cleanLabel: unsafe characters out, trimmed, at most 64 characters, "unnamed" when empty."""
    return UNSAFE_LABEL.sub("", raw or "").strip()[:LABEL_MAX].strip() or "unnamed"


def parse_profile(body: Dict[str, Any]) -> Dict[str, Any]:
    """moshi's parseProfile: the fields present, or {"error": reason}. Empty role → None."""
    profile: Dict[str, Any] = {}
    if "harness" in body:
        if body["harness"] not in HARNESSES:
            return {"error": "harness: one of " + ", ".join(HARNESSES)}
        profile["harness"] = body["harness"]
    if "role" in body:
        role = body["role"]
        if role is not None and not isinstance(role, str):
            return {"error": "role: a string"}
        trimmed = (role or "").strip()
        if len(trimmed.encode("utf-16-le")) // 2 > ROLE_MAX:  # JS `.length`: UTF-16 units
            return {"error": f"role: at most {ROLE_MAX} characters"}
        if CONTROL.search(trimmed):
            return {"error": "role: no control characters"}
        profile["role"] = trimmed or None
    if "skill" in body:
        skill = body["skill"]
        if skill is not None and not (isinstance(skill, str) and NAME_RE.fullmatch(skill)):
            return {"error": "skill: letters, digits, _ and -, at most 64, starting with a letter or digit"}
        profile["skill"] = skill
    return profile


class MoshiState:
    """Everything the fake knows. Mutate it from tests; the handler holds `lock` for every request."""

    def __init__(self) -> None:
        self.lock = threading.RLock()
        self.url = ""     # the MCP URL as config stores it: http://127.0.0.1:<port>/mcp
        self.origin = ""  # http://127.0.0.1:<port>
        self.requests: List[Dict[str, Any]] = []  # {method, path, auth, body}
        self.installations: Dict[str, Dict[str, Any]] = {}       # id -> {id, label, device_key, revoked}
        self.flows: Dict[str, Dict[str, Any]] = {}               # device_code -> flow
        self.registration_tokens: Dict[str, Dict[str, Any]] = {}  # rt_ -> {installation_id, expires_at, revoked, created}
        self.agents: Dict[str, Dict[str, Any]] = {}              # lower-case name -> agent
        self.agent_tokens: Dict[str, Dict[str, Any]] = {}        # bt_ -> {name, installation_id, revoked}
        self.admin_token = "mesh_admin_" + secrets.token_urlsafe(16)
        self.authorize_count = 0
        self.slow_down = 0                # the next n polls of a pending flow answer slow_down
        self.enforce_interval = False     # moshi's rule: a poll within the interval is slow_down, +5 s
        self.redirect_to = ""             # when set, every request answers `redirect_status` to this origin
        self.redirect_status = 307

    # -- seeding ----------------------------------------------------------------------------------

    def add_installation(self, label: str) -> Tuple[Dict[str, Any], str]:
        """An installation that went through the device flow; returns it and its device key."""
        with self.lock:
            return self._new_installation(label)

    def _new_installation(self, label: str) -> Tuple[Dict[str, Any], str]:
        key = "dk_" + secrets.token_urlsafe(32)
        installation = {"id": _ulid().upper(), "label": clean_label(label), "device_key": key, "revoked": False}
        self.installations[installation["id"]] = installation
        return installation, key

    def add_registration_token(self, installation_id: str, expired: bool = False) -> str:
        """A fresh registration token of the installation; like moshi, its other live ones are revoked."""
        with self.lock:
            token = self._issue_registration(installation_id)
            if expired:
                self.registration_tokens[token]["expires_at"] = _now() - timedelta(minutes=1)
            return token

    def _issue_registration(self, installation_id: str) -> str:
        for entry in self.registration_tokens.values():
            if entry["installation_id"] == installation_id:
                entry["revoked"] = True
        token = f"rt_{_ulid()}_" + "".join(secrets.choice(URLSAFE) for _ in range(43))
        self.registration_tokens[token] = {"installation_id": installation_id, "revoked": False, "created": 0,
                                           "expires_at": _now() + timedelta(hours=24)}
        return token

    def add_agent(self, name: str, kind: str = "local", harness: Optional[str] = "codex", role: Optional[str] = None,
                  skill: Optional[str] = None, presence: str = "offline",
                  installation_id: Optional[str] = None, active: bool = True) -> str:
        """An agent and one token: an installation token when `installation_id` is given, else its legacy
        token (what adopt takes, and what /api/agent/* answers 409 legacy_token to)."""
        with self.lock:
            agent = {"id": _ulid().upper(), "name": name, "kind": kind, "harness": harness, "role": role,
                     "skill": skill, "presence": presence, "working_on": None, "last_seen_at": None,
                     "is_active": active, "active_installation_id": None, "legacy_token": None}
            self.agents[name.lower()] = agent
            token = "bt_" + secrets.token_urlsafe(32)
            if installation_id:
                self.agent_tokens[token] = {"name": name.lower(), "installation_id": installation_id, "revoked": False}
            else:
                agent["legacy_token"] = token
            return token

    # -- driving and reading ----------------------------------------------------------------------

    def agent(self, name: str) -> Optional[Dict[str, Any]]:
        return self.agents.get(name.lower())

    def flow_by_user_code(self, user_code: str) -> Dict[str, Any]:
        """By the code as shown ("BCDF-GHJK") or without the dash."""
        code = user_code.replace("-", "")
        return next(flow for flow in self.flows.values() if flow["user_code"] == code)

    def approve(self, user_code: str) -> None:
        self.flow_by_user_code(user_code)["status"] = "approved"

    def deny(self, user_code: str) -> None:
        self.flow_by_user_code(user_code)["status"] = "denied"

    def expire(self, user_code: str) -> None:
        self.flow_by_user_code(user_code)["status"] = "expired"

    def revoke_installation(self, installation_id: str) -> None:
        """Like the dashboard: its device key, agent tokens and registration tokens stop; agents are released."""
        with self.lock:
            self.installations[installation_id]["revoked"] = True
            for entry in list(self.agent_tokens.values()) + list(self.registration_tokens.values()):
                if entry["installation_id"] == installation_id:
                    entry["revoked"] = True
            for agent in self.agents.values():
                if agent["active_installation_id"] == installation_id:
                    agent["active_installation_id"] = None

    def calls(self, method: str, path: str) -> List[Dict[str, Any]]:
        return [r for r in self.requests if r["method"] == method and r["path"] == path]

    def label_of(self, installation_id: Optional[str]) -> Optional[str]:
        installation = self.installations.get(installation_id or "")
        return installation["label"] if installation and not installation["revoked"] else None

    def live_installation(self, device_key: str) -> Optional[Dict[str, Any]]:
        return next((i for i in self.installations.values()
                     if device_key and i["device_key"] == device_key and not i["revoked"]), None)

    def issue_agent_token(self, name: str, installation_id: str) -> str:
        """A new installation token; the agent's previous one on that installation stops working."""
        for entry in self.agent_tokens.values():
            if entry["name"] == name.lower() and entry["installation_id"] == installation_id:
                entry["revoked"] = True
        token = "bt_" + secrets.token_urlsafe(32)
        self.agent_tokens[token] = {"name": name.lower(), "installation_id": installation_id, "revoked": False}
        return token


class FakeMoshi(BaseHTTPRequestHandler):
    """moshi's device, registration, installation and agent-self routes. State: `self.server.moshi`."""

    server_version = "FakeMoshi/1.4.0"

    def do_GET(self):  # noqa: N802 (http.server API)
        self._dispatch("GET")

    def do_POST(self):  # noqa: N802
        self._dispatch("POST")

    def do_PATCH(self):  # noqa: N802
        self._dispatch("PATCH")

    def do_DELETE(self):  # noqa: N802
        self._dispatch("DELETE")

    def log_message(self, *args):
        pass

    # -- plumbing ---------------------------------------------------------------------------------

    @property
    def state(self) -> MoshiState:
        return self.server.moshi  # type: ignore[attr-defined]

    def _dispatch(self, method: str) -> None:
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length) if length else b""
        try:
            self.body = json.loads(raw) if raw else None  # None: no body, or no JSON
            self.valid_json = bool(raw)
        except ValueError:
            self.body, self.valid_json = None, False
        path = self.path.split("?", 1)[0]
        state = self.state
        with state.lock:
            state.requests.append({"method": method, "path": path, "auth": self.headers.get("Authorization"),
                                   "body": self.body if self.valid_json else (raw.decode(errors="replace") or None)})
            if state.redirect_to:
                self._send(state.redirect_status, None, {"Location": state.redirect_to + path})
                return
            status, payload = self._route(method, path)
        headers = {"WWW-Authenticate": 'Bearer resource_metadata="x"'} if payload == {"error": "Unauthorized"} else {}
        self._send(status, payload, headers)

    def _route(self, method: str, path: str) -> Answer:
        if path.startswith("/api/device/"):
            device = {("POST", "/api/device/authorize"): self._authorize, ("POST", "/api/device/token"): self._token}
            handler = device.get((method, path))
            return handler() if handler else (404, {"error": "Not Found"})
        if path.startswith("/api/register/"):
            return self._register_routes(method, path)
        who = self._middleware(path)
        if isinstance(who, tuple):
            return who
        if path.startswith("/api/installation/"):
            if who.get("role") != "device":
                return 403, {"error": "device_key_required"}
            return self._status(who) if (method, path) == ("GET", "/api/installation/status") else (404, {"error": "Not Found"})
        if path.startswith("/api/agent/"):
            routes = {("POST", "/api/agent/activate"): self._activate, ("PATCH", "/api/agent/profile"): self._profile,
                      ("DELETE", "/api/agent/token"): self._drop_token}
            handler = routes.get((method, path))
            if handler is None:
                return 404, {"error": "Not Found"}
            if who.get("role") != "agent":
                return 403, {"error": "installation_token_required"}
            if not who.get("installation_id"):
                return 409, {"error": "legacy_token"}
            return handler(who)
        return 404, {"error": "Not Found"}

    def _bearer(self) -> Optional[str]:
        header = self.headers.get("Authorization")
        if header is None:
            return None
        return header[len("Bearer "):] if header.startswith("Bearer ") else ""

    def _json_object(self) -> Optional[Dict[str, Any]]:
        return self.body if isinstance(self.body, dict) else None

    def _send(self, status: int, payload: Optional[Dict[str, Any]], headers: Optional[Dict[str, str]] = None) -> None:
        data = json.dumps(payload).encode() if payload is not None else b""
        self.send_response(status)
        if payload is not None:
            self.send_header("Content-Type", "application/json")
        for name, value in (headers or {}).items():
            self.send_header(name, value)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    # -- the auth middleware (everything but /api/device and /api/register) -----------------------

    def _middleware(self, path: str):
        """The caller as {role, name?, installation_id?}, or the refusing answer."""
        state, bearer = self.state, self._bearer() or ""
        if bearer and bearer == state.admin_token:
            return {"role": "admin"}
        installation = state.live_installation(bearer) if bearer.startswith("dk_") else None
        if installation:
            if not path.startswith("/api/installation/"):
                return 403, {"error": "device_key_scope"}
            return {"role": "device", "installation_id": installation["id"]}
        entry = state.agent_tokens.get(bearer)
        if entry and not entry["revoked"] and state.label_of(entry["installation_id"]) is not None:
            agent = state.agents.get(entry["name"])
            if agent and agent["is_active"]:
                return {"role": "agent", "name": entry["name"], "installation_id": entry["installation_id"]}
        legacy = next((a for a in state.agents.values() if bearer and a["legacy_token"] == bearer), None)
        if legacy and legacy["is_active"]:
            return {"role": "agent", "name": legacy["name"].lower(), "installation_id": None}
        return 401, {"error": "Unauthorized"}

    # -- device flow ------------------------------------------------------------------------------

    def _authorize(self) -> Answer:
        state = self.state
        state.authorize_count += 1
        if state.authorize_count > AUTHORIZE_LIMIT:
            return 429, {"error": "too_many_requests"}
        renew = None
        bearer = self._bearer()
        if bearer is not None:
            renew = state.live_installation(bearer) if bearer.startswith("dk_") else None
            if renew is None:
                return 401, {"error": "invalid_device_key"}
        body = self._json_object()
        label = body.get("label") if body else None
        if body is None or ("label" in body and not isinstance(label, str)):
            return 400, {"error": "invalid_request"}
        device_code = secrets.token_urlsafe(32)
        user_code = "".join(secrets.choice("BCDFGHJKLMNPQRSTVWXZ") for _ in range(8))
        state.flows[device_code] = {
            "user_code": user_code, "status": "pending", "interval": DEVICE_INTERVAL, "last_poll": None,
            "label": renew["label"] if renew else clean_label(label or ""),  # a renewal keeps its approved name
            "installation_id": renew["id"] if renew else None}
        shown = f"{user_code[:4]}-{user_code[4:]}"
        uri = f"{state.origin}/link"
        return 200, {"device_code": device_code, "user_code": shown, "verification_uri": uri,
                     "verification_uri_complete": f"{uri}?code={shown}", "expires_in": DEVICE_EXPIRES_IN,
                     "interval": DEVICE_INTERVAL}

    def _token(self) -> Answer:
        state = self.state
        body = self._json_object()
        device_code = body.get("device_code") if body else None
        if not isinstance(device_code, str) or not device_code:
            return 400, {"error": "invalid_request"}
        flow = state.flows.get(device_code)
        if flow is None or flow["status"] in ("expired", "redeemed"):
            return 400, {"error": "expired_token"}
        if flow["status"] == "denied":
            return 400, {"error": "access_denied"}
        if flow["status"] == "pending":
            now, last = time.monotonic(), flow["last_poll"]
            flow["last_poll"] = now
            if state.slow_down > 0 or (state.enforce_interval and last is not None
                                       and now - last < flow["interval"]):
                state.slow_down = max(0, state.slow_down - 1)
                flow["interval"] += 5
                return 400, {"error": "slow_down"}
            return 400, {"error": "authorization_pending"}
        flow["status"] = "redeemed"  # approved: redeemed exactly once
        device_key: Optional[str] = None
        if flow["installation_id"] is None:
            installation, device_key = state._new_installation(flow["label"])
        else:
            installation = state.installations[flow["installation_id"]]
            if installation["revoked"]:
                return 400, {"error": "access_denied"}
        token = state._issue_registration(installation["id"])
        return 200, {"device_key": device_key,
                     "installation": {"id": installation["id"], "label": installation["label"]},
                     "registration_token": token,
                     "registration_expires_at": _iso(state.registration_tokens[token]["expires_at"])}

    def _status(self, who: Dict[str, Any]) -> Answer:
        state = self.state
        agents = [{"name": a["name"], "role": a["role"], "presence": a["presence"], "working_on": a["working_on"],
                   "last_seen_at": a["last_seen_at"], "kind": a["kind"], "harness": a["harness"],
                   "skill": a["skill"], "active_installation": state.label_of(a["active_installation_id"])}
                  for a in state.agents.values() if a["is_active"]]
        return 200, {"installation": {"id": who["installation_id"], "label": state.label_of(who["installation_id"])},
                     "agents": agents}

    # -- /api/register/* (registration token only) ------------------------------------------------

    def _register_routes(self, method: str, path: str) -> Answer:
        header = self.headers.get("Authorization") or ""
        if not header.startswith("Bearer rt_"):
            return 401, {"error": "registration_token_required"}
        prefix, suffix = "/api/register/agents/", "/load"
        if method == "POST" and path.startswith(prefix) and path.endswith(suffix):
            return self._load(unquote(path[len(prefix):-len(suffix)]).strip())
        if (method, path) not in (("POST", "/api/register/agents"), ("POST", "/api/register/agents/adopt")):
            return 404, {"error": "Not Found"}
        if not self.valid_json:
            return 400, {"error": "invalid_json"}
        body = self._json_object()
        if body is None:
            return 400, {"error": "invalid_body"}
        return self._adopt(body) if path.endswith("/adopt") else self._register(body)

    def _registration(self):
        """The presented registration token's entry, or moshi's refusal of it."""
        bearer = self._bearer() or ""
        entry = self.state.registration_tokens.get(bearer) if RT_RE.fullmatch(bearer) else None
        if entry is None:
            return 401, {"error": "invalid_token"}
        if entry["revoked"] or self.state.label_of(entry["installation_id"]) is None:
            return 401, {"error": "token_revoked"}
        if entry["expires_at"] <= _now():
            return 401, {"error": "token_expired"}
        return entry

    def _register(self, body: Dict[str, Any]) -> Answer:
        entry = self._registration()
        if isinstance(entry, tuple):
            return entry
        state = self.state
        name = body["name"].strip() if isinstance(body.get("name"), str) else ""
        if not NAME_RE.fullmatch(name):
            return 400, {"error": "invalid_name"}
        profile = parse_profile(body)
        if "error" in profile or "harness" not in profile:
            return 400, {"error": "invalid_profile", "message": profile.get("error", "harness: required")}
        if entry["created"] >= REGISTER_PER_HOUR:
            return 429, {"error": "rate_limited"}
        if name.lower() in RESERVED_NAMES:
            return 400, {"error": "registration_failed"}
        if name.lower() in state.agents:
            return 409, {"error": "name_taken", "message": f"There's already an agent called {name}. Pick another name."}
        entry["created"] += 1
        state.add_agent(name, kind="local", harness=profile["harness"], role=profile.get("role"),
                        skill=profile.get("skill"), installation_id=None)
        agent = state.agents[name.lower()]
        agent["legacy_token"] = None  # its legacy hash becomes one nobody holds the token for
        agent["active_installation_id"] = entry["installation_id"]
        token = state.issue_agent_token(name, entry["installation_id"])
        keys = ("id", "name", "kind", "harness", "role", "skill")
        return 201, {"agent": {key: agent[key] for key in keys}, "token": token}

    def _load(self, name: str) -> Answer:
        entry = self._registration()
        if isinstance(entry, tuple):
            return entry
        state = self.state
        agent = state.agent(name) if name else None
        if agent is None or not agent["is_active"]:
            return 404, {"error": "not_found"}
        if agent["kind"] != "local":
            return 409, {"error": "not_local"}
        token = state.issue_agent_token(agent["name"], entry["installation_id"])
        profile = {key: agent[key] for key in ("id", "name", "kind", "harness", "role", "skill")}
        profile["active_installation"] = state.label_of(agent["active_installation_id"])  # unchanged by a load
        return 200, {"agent": profile, "token": token}

    def _adopt(self, body: Dict[str, Any]) -> Answer:
        entry = self._registration()
        if isinstance(entry, tuple):
            return entry
        state = self.state
        profile = parse_profile({"harness": body["harness"]} if "harness" in body else {})
        if "error" in profile or "harness" not in profile:
            return 400, {"error": "invalid_profile", "message": profile.get("error", "harness: required")}
        legacy = body.get("token")
        if not isinstance(legacy, str) or not legacy:
            return 400, {"error": "invalid_token"}
        if legacy == state.admin_token:
            return 400, {"error": "admin_token_refused"}
        if not legacy.startswith("bt_"):
            return 400, {"error": "invalid_token"}
        if legacy in state.agent_tokens:
            return 409, {"error": "already_adopted"}
        agent = next((a for a in state.agents.values() if a["legacy_token"] == legacy), None)
        if agent is None:
            return 404, {"error": "not_found"}
        if not agent["is_active"]:
            return 409, {"error": "agent_inactive"}
        for token_entry in state.agent_tokens.values():
            if token_entry["name"] == agent["name"].lower() and token_entry["installation_id"] == entry["installation_id"]:
                token_entry["revoked"] = True
        state.agent_tokens[legacy] = {"name": agent["name"].lower(), "installation_id": entry["installation_id"],
                                      "revoked": False}
        agent.update(legacy_token=None, kind="local", harness=profile["harness"],
                     active_installation_id=entry["installation_id"])
        return 200, {"agent": {"name": agent["name"], "kind": "local", "harness": profile["harness"]}}

    # -- /api/agent/* (the agent's installation token) --------------------------------------------

    def _activate(self, who: Dict[str, Any]) -> Answer:
        agent = self.state.agents[who["name"]]
        previous = agent["active_installation_id"]
        agent["active_installation_id"] = who["installation_id"]
        return 200, {"previous": self.state.label_of(previous)}  # also when it was this installation

    def _profile(self, who: Dict[str, Any]) -> Answer:
        body = self._json_object()
        if body is None:
            return 400, {"error": "invalid_body"}
        profile = parse_profile({key: body[key] for key in ("role", "skill") if key in body})
        if "error" in profile:
            return 400, {"error": "invalid_profile", "message": profile["error"]}
        agent = self.state.agents[who["name"]]
        agent.update(profile)
        return 200, {"agent": {"name": agent["name"], "role": agent["role"], "skill": agent["skill"]}}

    def _drop_token(self, who: Dict[str, Any]) -> Answer:
        state = self.state
        for token_entry in state.agent_tokens.values():
            if token_entry["name"] == who["name"] and token_entry["installation_id"] == who["installation_id"]:
                token_entry["revoked"] = True
        agent = state.agents[who["name"]]
        if agent["active_installation_id"] == who["installation_id"]:
            agent["active_installation_id"] = None
        return 200, {"dropped": True}


def serve(test_case) -> MoshiState:
    """Starts a FakeMoshi on a free local port for one test; stopped by the test's cleanup."""
    state = MoshiState()
    server = ThreadingHTTPServer(("127.0.0.1", 0), FakeMoshi)
    server.daemon_threads = True
    server.moshi = state  # type: ignore[attr-defined]
    state.origin = f"http://127.0.0.1:{server.server_port}"
    state.url = state.origin + "/mcp"
    thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True)
    thread.start()

    def stop() -> None:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)

    test_case.addCleanup(stop)
    return state
