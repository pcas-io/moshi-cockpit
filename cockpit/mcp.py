"""Minimal MCP client over Streamable HTTP (stdlib only). Used for status, the agent-test restore and endpoint checks."""

from __future__ import annotations

import http.client
import json
import urllib.error
import urllib.request
from typing import Any, Callable, Dict, Optional, Tuple

from cockpit.http import MAX_BODY, HttpFailure, no_redirect_opener, send
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
        message = json.loads(body)
    except (ValueError, RecursionError):  # RecursionError: JSON nested deeper than the parser goes
        raise McpError("Antwort des Servers ist kein gültiges JSON") from None
    if not isinstance(message, dict):
        raise McpError("The server's answer is no JSON-RPC message")
    return message


class McpClient:
    def __init__(self, url: str, token: str, opener: Optional[Opener] = None, timeout: float = 20) -> None:
        """Without an `opener`, redirects are refused: urllib would forward the bearer to any origin."""
        self._url = url
        self._token = token
        self._opener = opener or no_redirect_opener()
        self._timeout = timeout
        self._session: Optional[str] = None
        self._next_id = 1

    def call_tool(self, name: str, arguments: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        """Initializes on first use and returns the tool's JSON result (parsed from its text content)."""
        if self._session is None:
            self._initialize()
        message = self._request("tools/call", {"name": name, "arguments": arguments or {}})
        result = message.get("result") or {}
        if not isinstance(result, dict):
            raise McpError(f"Tool {name}: the server's result is no object")
        if result.get("isError"):
            raise McpError(f"Tool {name} meldet einen Fehler: {clean(_first_text(result), limit=200)}")
        text = _first_text(result)
        try:
            return json.loads(text) if text else {}
        except ValueError:
            return {"text": text}
        except RecursionError:  # nested deeper than the parser goes
            raise McpError(f"Tool {name}: the server's result is nested too deep") from None

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
            err = message["error"] if isinstance(message["error"], dict) else {}
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
            reply = send(self._opener, request, self._timeout)
        except HttpFailure as failure:
            raise McpError(self._explain(failure)) from None
        session = reply.headers.get("Mcp-Session-Id")
        if session:
            self._session = session
        elif self._session is None:
            self._session = ""  # stateless server: remember that initialize happened
        body = reply.body.decode("utf-8", errors="replace")
        return reply.status, parse_body(reply.headers.get("Content-Type", ""), body)

    def _explain(self, failure: HttpFailure) -> str:
        if failure.kind == "redirect":
            return f"HTTP {failure.status} from {self._url}: redirect refused, not followed"
        if failure.kind == "http":
            return f"HTTP {failure.status} von {self._url}"
        if failure.kind == "unreachable":
            return f"{self._url} nicht erreichbar"
        return f"{self._url} answered without usable HTTP (garbage, cut off or over {MAX_BODY // 1024} KiB)"


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
    except OSError:  # a timeout or a dropped connection outside URLError
        raise McpError(f"{url} nicht erreichbar") from None
    except http.client.HTTPException:
        raise McpError(f"{url} answered without usable HTTP") from None


def _first_text(result: Dict[str, Any]) -> str:
    content = result.get("content")
    for item in content if isinstance(content, list) else []:
        if isinstance(item, dict) and item.get("type") == "text":
            return str(item.get("text", ""))
    return ""
