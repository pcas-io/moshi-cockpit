"""An in-memory moshi (1.4.0 API) for tests: device flow, installation status, agent registration.

Start one with `serve(test_case)`; it returns the `MoshiState` the handler works on. Tests seed and
inspect that state directly (`approve`, `add_agent`, `requests`, ...). Every request is recorded with
its Authorization header, so a test can prove which token went where, or that nothing was called.
"""

from __future__ import annotations

import json
import secrets
import string
import threading
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import unquote

ULID_ALPHABET = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"
URLSAFE = string.ascii_letters + string.digits + "-_"


def _secret(prefix: str) -> str:
    """Same shape as moshi's tokens: <prefix>_<26 ULID chars>_<43 url-safe chars>."""
    ulid = "".join(secrets.choice(ULID_ALPHABET) for _ in range(26))
    return f"{prefix}_{ulid}_" + "".join(secrets.choice(URLSAFE) for _ in range(43))


def _iso(moment: datetime) -> str:
    return moment.strftime("%Y-%m-%dT%H:%M:%SZ")


class MoshiState:
    """Everything the fake knows. Mutate it from tests; the handler takes `lock` for every request."""

    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.url = ""     # the MCP URL as config stores it: http://127.0.0.1:<port>/mcp
        self.origin = ""  # http://127.0.0.1:<port>
        self.requests: List[Dict[str, Any]] = []  # {method, path, auth, body}
        self.installations: Dict[str, Dict[str, Any]] = {}   # id -> {id, label, device_key}
        self.flows: Dict[str, Dict[str, Any]] = {}           # device_code -> flow
        self.registration_tokens: Dict[str, Dict[str, Any]] = {}  # rt_ -> {installation_id, expires_at}
        self.agents: Dict[str, Dict[str, Any]] = {}          # lower-case name -> profile
        self.agent_tokens: Dict[str, Dict[str, Any]] = {}    # token -> {name, installation_id or None}
        self.registration_ttl = timedelta(hours=24)
        self.too_many_requests = False  # authorize answers 429
        self.slow_down = 0              # the next n token polls answer slow_down
        self.redirect_to = ""           # when set, every request answers `redirect_status` to this origin
        self.redirect_status = 307
        self._ids = 0

    # -- seeding and driving the fake from tests --------------------------------------------------

    def next_id(self, prefix: str) -> str:
        self._ids += 1
        return f"{prefix}{self._ids:04d}"

    def add_installation(self, label: str) -> Tuple[Dict[str, Any], str]:
        """An installation that already went through the device flow; returns it and its device key."""
        with self.lock:
            return self._new_installation(label)

    def _new_installation(self, label: str) -> Tuple[Dict[str, Any], str]:
        installation = {"id": self.next_id("inst_"), "label": label, "device_key": _secret("dk")}
        self.installations[installation["id"]] = installation
        return installation, installation["device_key"]

    def add_registration_token(self, installation_id: str, expired: bool = False) -> str:
        with self.lock:
            return self._new_registration_token(installation_id, expired)

    def _new_registration_token(self, installation_id: str, expired: bool = False) -> str:
        token = _secret("rt")
        delta = -timedelta(minutes=1) if expired else self.registration_ttl
        self.registration_tokens[token] = {"installation_id": installation_id,
                                           "expires_at": datetime.now(timezone.utc) + delta}
        return token

    def add_agent(self, name: str, kind: str = "local", harness: str = "codex", role: str = "",
                  skill: str = "", presence: str = "offline", installation_id: Optional[str] = None) -> str:
        """An agent with one token. `installation_id=None` makes it a legacy token (adopt, legacy_token)."""
        with self.lock:
            self.agents[name.lower()] = {
                "id": self.next_id("agt_"), "name": name, "kind": kind, "harness": harness, "role": role,
                "skill": skill, "presence": presence, "working_on": "", "last_seen_at": None,
                "active_installation": None, "adopted": installation_id is not None}
            token = _secret("bt")
            self.agent_tokens[token] = {"name": name.lower(), "installation_id": installation_id}
            return token

    def agent(self, name: str) -> Optional[Dict[str, Any]]:
        return self.agents.get(name.lower())

    def flow_by_user_code(self, user_code: str) -> Dict[str, Any]:
        return next(flow for flow in self.flows.values() if flow["user_code"] == user_code)

    def approve(self, user_code: str) -> None:
        self.flow_by_user_code(user_code)["status"] = "approved"

    def deny(self, user_code: str) -> None:
        self.flow_by_user_code(user_code)["status"] = "denied"

    def expire(self, user_code: str) -> None:
        self.flow_by_user_code(user_code)["status"] = "expired"

    def revoke_installation(self, installation_id: str) -> None:
        """Like revoking the device in the dashboard: its device key stops working."""
        self.installations[installation_id]["device_key"] = ""

    def calls(self, method: str, path: str) -> List[Dict[str, Any]]:
        return [r for r in self.requests if r["method"] == method and r["path"] == path]

    def label_of(self, installation_id: Optional[str]) -> Optional[str]:
        installation = self.installations.get(installation_id or "")
        return installation["label"] if installation else None


class FakeMoshi(BaseHTTPRequestHandler):
    """Routes of moshi's device and registration API. State lives on `self.server.moshi`."""

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
            body = json.loads(raw) if raw else None  # None: the request had no body
        except ValueError:
            body = ValueError
        path = self.path.split("?", 1)[0]
        state = self.state
        with state.lock:
            state.requests.append({"method": method, "path": path, "auth": self.headers.get("Authorization"),
                                   "body": "<invalid json>" if body is ValueError else body})
            if state.redirect_to:
                self._redirect(state.redirect_status, state.redirect_to + path)
                return
            if body is not None and not isinstance(body, dict):
                self._send(400, {"error": "invalid_json"})
                return
            status, payload = self._route(method, path, body or {})
        self._send(status, payload)

    def _route(self, method: str, path: str, body: Dict[str, Any]) -> Tuple[int, Optional[Dict[str, Any]]]:
        routes = {
            ("POST", "/api/device/authorize"): self._authorize,
            ("POST", "/api/device/token"): self._token,
            ("GET", "/api/installation/status"): self._status,
            ("POST", "/api/register/agents"): self._register,
            ("POST", "/api/register/agents/adopt"): self._adopt,
            ("POST", "/api/agent/activate"): self._activate,
            ("PATCH", "/api/agent/profile"): self._profile,
            ("DELETE", "/api/agent/token"): self._drop_token,
        }
        handler = routes.get((method, path))
        if handler:
            return handler(body)
        prefix, suffix = "/api/register/agents/", "/load"
        if method == "POST" and path.startswith(prefix) and path.endswith(suffix):
            return self._load(unquote(path[len(prefix):-len(suffix)]))
        return 404, {"error": "not_found"}

    def _bearer(self) -> str:
        header = self.headers.get("Authorization") or ""
        return header[len("Bearer "):] if header.startswith("Bearer ") else ""

    def _send(self, status: int, payload: Optional[Dict[str, Any]]) -> None:
        data = json.dumps(payload).encode() if payload is not None else b""
        self.send_response(status)
        if payload is not None:
            self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _redirect(self, status: int, location: str) -> None:
        self.send_response(status)
        self.send_header("Location", location)
        self.send_header("Content-Length", "0")
        self.end_headers()

    # -- auth helpers -----------------------------------------------------------------------------

    def _installation_by_device_key(self) -> Optional[Dict[str, Any]]:
        key = self._bearer()
        if not key:
            return None
        return next((i for i in self.state.installations.values() if i["device_key"] == key), None)

    def _registration(self) -> Optional[Dict[str, Any]]:
        entry = self.state.registration_tokens.get(self._bearer())
        if not entry or entry["expires_at"] <= datetime.now(timezone.utc):
            return None
        return entry

    def _agent_token(self) -> Optional[Dict[str, Any]]:
        entry = self.state.agent_tokens.get(self._bearer())
        if not entry or entry["name"] not in self.state.agents:
            return None
        return entry

    # -- device flow ------------------------------------------------------------------------------

    def _authorize(self, body: Dict[str, Any]):
        state = self.state
        if state.too_many_requests:
            return 429, {"error": "too_many_requests"}
        renew: Optional[str] = None
        if self._bearer():
            installation = self._installation_by_device_key()
            if installation is None:
                return 401, {"error": "invalid_device_key"}
            renew = installation["id"]
        label = str(body.get("label") or "").strip()
        if not label:
            return 400, {"error": "invalid_label"}
        device_code = secrets.token_urlsafe(24)
        user_code = "".join(secrets.choice("BCDFGHJKLMNPQRSTVWXZ") for _ in range(8))
        user_code = f"{user_code[:4]}-{user_code[4:]}"
        state.flows[device_code] = {"user_code": user_code, "label": label, "status": "pending",
                                    "renew": renew}
        uri = f"{state.origin}/device"
        return 200, {"device_code": device_code, "user_code": user_code, "verification_uri": uri,
                     "verification_uri_complete": f"{uri}?code={user_code}", "expires_in": 900, "interval": 5}

    def _token(self, body: Dict[str, Any]):
        state = self.state
        flow = state.flows.get(str(body.get("device_code") or ""))
        if flow is None:
            return 400, {"error": "expired_token"}
        if state.slow_down > 0:
            state.slow_down -= 1
            return 400, {"error": "slow_down"}
        outcome = {"pending": "authorization_pending", "denied": "access_denied", "expired": "expired_token",
                   "used": "expired_token"}.get(flow["status"])
        if outcome:
            return 400, {"error": outcome}
        flow["status"] = "used"
        if flow["renew"]:
            installation, device_key = state.installations[flow["renew"]], None
        else:
            installation, device_key = state._new_installation(flow["label"])
        token = state._new_registration_token(installation["id"])
        expires = state.registration_tokens[token]["expires_at"]
        return 200, {"device_key": device_key,
                     "installation": {"id": installation["id"], "label": installation["label"]},
                     "registration_token": token, "registration_expires_at": _iso(expires)}

    def _status(self, _body: Dict[str, Any]):
        installation = self._installation_by_device_key()
        if installation is None:
            return 401, {"error": "invalid_device_key"}
        keys = ("name", "role", "presence", "working_on", "last_seen_at", "kind", "harness", "skill",
                "active_installation")
        agents = [{key: agent.get(key) for key in keys} for agent in self.state.agents.values()]
        return 200, {"installation": {"id": installation["id"], "label": installation["label"]}, "agents": agents}

    # -- registration (installation-scoped rt_) ---------------------------------------------------

    def _register(self, body: Dict[str, Any]):
        entry = self._registration()
        if entry is None:
            return 401, {"error": "invalid_registration_token"}
        name, harness = str(body.get("name") or ""), str(body.get("harness") or "")
        if not name or not harness:
            return 400, {"error": "invalid_request"}
        state = self.state
        if name.lower() in state.agents:
            return 409, {"error": "name_taken"}
        agent = {"id": state.next_id("agt_"), "name": name, "kind": "local", "harness": harness,
                 "role": str(body.get("role") or ""), "skill": str(body.get("skill") or ""),
                 "presence": "offline", "working_on": "", "last_seen_at": None, "active_installation": None,
                 "adopted": True}
        state.agents[name.lower()] = agent
        token = _secret("bt")
        state.agent_tokens[token] = {"name": name.lower(), "installation_id": entry["installation_id"]}
        keys = ("id", "name", "kind", "harness", "role", "skill")
        return 201, {"agent": {key: agent[key] for key in keys}, "token": token}

    def _load(self, name: str):
        entry = self._registration()
        if entry is None:
            return 401, {"error": "invalid_registration_token"}
        state = self.state
        agent = state.agent(name)
        if agent is None:
            return 404, {"error": "not_found"}
        if agent["kind"] != "local":
            return 409, {"error": "not_local"}
        token = _secret("bt")
        state.agent_tokens[token] = {"name": name.lower(), "installation_id": entry["installation_id"]}
        keys = ("id", "name", "kind", "harness", "role", "skill")
        profile = {key: agent[key] for key in keys}
        profile["active_installation"] = agent["active_installation"]
        return 200, {"agent": profile, "token": token}

    def _adopt(self, body: Dict[str, Any]):
        entry = self._registration()
        if entry is None:
            return 401, {"error": "invalid_registration_token"}
        harness = str(body.get("harness") or "")
        legacy = self.state.agent_tokens.get(str(body.get("token") or ""))
        if not harness:
            return 400, {"error": "invalid_request"}
        if legacy is None or legacy["name"] not in self.state.agents:
            return 401, {"error": "invalid_token"}
        agent = self.state.agents[legacy["name"]]
        if agent["adopted"] or legacy["installation_id"]:
            return 409, {"error": "already_adopted"}
        agent.update(adopted=True, kind="local", harness=harness)
        legacy["installation_id"] = entry["installation_id"]  # the token now belongs to this installation
        return 200, {"agent": {"name": agent["name"], "kind": agent["kind"], "harness": agent["harness"]}}

    # -- the agent's own token --------------------------------------------------------------------

    def _activate(self, _body: Dict[str, Any]):
        entry = self._agent_token()
        if entry is None:
            return 401, {"error": "invalid_token"}
        if not entry["installation_id"]:
            return 409, {"error": "legacy_token"}
        agent = self.state.agents[entry["name"]]
        label = self.state.label_of(entry["installation_id"])
        previous = agent["active_installation"]
        agent["active_installation"] = label
        return 200, {"previous": previous if previous != label else None}

    def _profile(self, body: Dict[str, Any]):
        entry = self._agent_token()
        if entry is None:
            return 401, {"error": "invalid_token"}
        agent = self.state.agents[entry["name"]]
        for key in ("role", "skill"):
            if key in body:
                agent[key] = str(body[key] or "")
        keys = ("id", "name", "kind", "harness", "role", "skill")
        return 200, {"agent": {key: agent[key] for key in keys}}

    def _drop_token(self, _body: Dict[str, Any]):
        if self._agent_token() is None:
            return 401, {"error": "invalid_token"}
        del self.state.agent_tokens[self._bearer()]
        return 204, None


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
