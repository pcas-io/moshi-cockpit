"""Minimal MCP client over Streamable HTTP (stdlib only). Used for status, the agent-test restore and endpoint checks."""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from typing import Any, Callable, Dict, Optional, Tuple

from cockpit.paths import USER_AGENT
from cockpit.text import clean

PROTOCOL_VERSION = "2025-06-18"
Opener = Callable[..., Any]


class McpError(RuntimeError):
    """Never carries the bearer token; messages are safe to print."""


def parse_body(content_type: str, body: str) -> Dict[str, Any]:
    """Returns the JSON-RPC message from a JSON or an SSE response (last `data:` event wins)."""
    if "text/event-stream" in (content_type or ""):
        payload = ""
        for line in body.splitlines():
            if line.startswith("data:"):
                payload = line[5:].strip()
        body = payload
    if not body.strip():
        return {}
    try:
        return json.loads(body)
    except ValueError as exc:
        raise McpError("Antwort des Servers ist kein gültiges JSON") from exc


class McpClient:
    def __init__(self, url: str, token: str, opener: Opener = urllib.request.urlopen, timeout: float = 20) -> None:
        self._url = url
        self._token = token
        self._opener = opener
        self._timeout = timeout
        self._session: Optional[str] = None
        self._next_id = 1

    def call_tool(self, name: str, arguments: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        """Initializes on first use and returns the tool's JSON result (parsed from its text content)."""
        if self._session is None:
            self._initialize()
        message = self._request("tools/call", {"name": name, "arguments": arguments or {}})
        result = message.get("result") or {}
        if result.get("isError"):
            raise McpError(f"Tool {name} meldet einen Fehler: {clean(_first_text(result), limit=200)}")
        text = _first_text(result)
        try:
            return json.loads(text) if text else {}
        except ValueError:
            return {"text": text}

    def _initialize(self) -> None:
        params = {"protocolVersion": PROTOCOL_VERSION, "capabilities": {},
                  "clientInfo": {"name": "moshi-cockpit", "version": "0.1"}}
        self._request("initialize", params)
        self._post({"jsonrpc": "2.0", "method": "notifications/initialized"})

    def _request(self, method: str, params: Dict[str, Any]) -> Dict[str, Any]:
        request_id = self._next_id
        self._next_id += 1
        _, message = self._post({"jsonrpc": "2.0", "id": request_id, "method": method, "params": params})
        if "error" in message:
            err = message["error"] or {}
            raise McpError(f"{method}: {clean(err.get('message'), 'Fehler', limit=200)}")
        return message

    def _post(self, payload: Dict[str, Any]) -> Tuple[int, Dict[str, Any]]:
        headers = {
            "Authorization": f"Bearer {self._token}",
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
            "User-Agent": USER_AGENT,
            "MCP-Protocol-Version": PROTOCOL_VERSION,
        }
        if self._session:
            headers["Mcp-Session-Id"] = self._session
        request = urllib.request.Request(self._url, data=json.dumps(payload).encode(), headers=headers, method="POST")
        try:
            with self._opener(request, timeout=self._timeout) as response:
                session = response.headers.get("Mcp-Session-Id")
                if session:
                    self._session = session
                elif self._session is None:
                    self._session = ""  # stateless server: remember that initialize happened
                body = response.read().decode("utf-8", errors="replace")
                return response.status, parse_body(response.headers.get("Content-Type", ""), body)
        except urllib.error.HTTPError as exc:
            exc.close()  # the error holds the open response
            raise McpError(f"HTTP {exc.code} von {self._url}") from None
        except urllib.error.URLError as exc:
            raise McpError(f"{self._url} nicht erreichbar ({exc.reason})") from None


def probe_unauthenticated(url: str, opener: Opener = urllib.request.urlopen) -> Tuple[int, str]:
    """POSTs an initialize without a token. A healthy endpoint answers 401 with a Bearer challenge."""
    body = json.dumps({"jsonrpc": "2.0", "id": 1, "method": "initialize",
                       "params": {"protocolVersion": PROTOCOL_VERSION, "capabilities": {},
                                  "clientInfo": {"name": "moshi-cockpit-doctor", "version": "0.1"}}}).encode()
    request = urllib.request.Request(url, data=body, method="POST", headers={
        "Content-Type": "application/json", "Accept": "application/json, text/event-stream", "User-Agent": USER_AGENT})
    try:
        with opener(request, timeout=10) as response:
            return response.status, response.headers.get("WWW-Authenticate", "")
    except urllib.error.HTTPError as exc:
        exc.close()  # the error holds the open response; status and headers stay readable
        return exc.code, exc.headers.get("WWW-Authenticate", "") if exc.headers else ""
    except urllib.error.URLError as exc:
        raise McpError(f"{url} nicht erreichbar ({exc.reason})") from None


def _first_text(result: Dict[str, Any]) -> str:
    for item in result.get("content") or []:
        if item.get("type") == "text":
            return str(item.get("text", ""))
    return ""
