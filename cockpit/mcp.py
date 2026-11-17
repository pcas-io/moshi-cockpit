"""The doctor's unauthenticated probe of an MCP endpoint (stdlib only). status reads moshi through its device
API instead, which changes no agent's presence."""

from __future__ import annotations

import http.client
import json
import urllib.error
import urllib.request
from typing import Any, Callable, Tuple

from cockpit.paths import USER_AGENT

PROTOCOL_VERSION = "2025-06-18"
Opener = Callable[..., Any]


class McpError(RuntimeError):
    """Never carries the bearer token; messages are safe to print."""


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
        raise McpError(f"{url} is not reachable ({exc.reason})") from None
    except OSError:  # a timeout or a dropped connection outside URLError
        raise McpError(f"{url} is not reachable") from None
    except http.client.HTTPException:
        raise McpError(f"{url} answered without usable HTTP") from None
