"""`moshi-cockpit ring <agent> <message id>`: the doorbell between agents on this machine.

The message itself goes through moshi first; ring only tells the recipient that it is there. It pastes exactly
one line, `Moshi <id> — <type> from <sender>`, into the one terminal the roster names for the recipient, and
only after moshi confirms, with the caller's own agent token, that the message exists and is addressed to that
recipient. No free text, no keys, no reading.

`--without-record` is for when moshi is down: then the text itself is pasted, marked `[no-record]`, and the
sender owes moshi the record once it is back. It is refused while moshi answers.
"""

from __future__ import annotations

import json
import os
import re
import urllib.request
from typing import Any, Callable, Dict, Mapping, Optional

from cockpit import roster as roster_mod
from cockpit.config import load_config
from cockpit.status import Mesh
from cockpit.harnesses.base import TOKEN_ENV
from cockpit.hc import Hc
from cockpit.http import HttpFailure, Opener, no_redirect_opener, parse_json, send
from cockpit.mcp import McpError, probe_unauthenticated
from cockpit.paths import USER_AGENT, Paths
from cockpit.text import clean

MESSAGE_ID = re.compile(r"msg_[0-9A-HJKMNP-TV-Z]{26}")
# moshi's own shape for a type that goes into a prompt (webhooks): identifier characters only.
TYPE_SHAPE = re.compile(r"[^A-Za-z0-9_.-]")
NO_RECORD = "[no-record]"
MAX_TEXT = 2000
NO_IDENTITY = ("this terminal has no moshi identity of its own; start the agent with moshi-cockpit run, "
               "or use --without-record while moshi is down")
UNREACHABLE = "moshi is not reachable; ring --without-record \"<text>\" and record it in moshi once it is back"

Fetch = Callable[[str, str, str], Dict[str, Any]]


def doorbell(message_id: str, message: Dict[str, Any]) -> str:
    kind = TYPE_SHAPE.sub("", str(message.get("type") or "info"))[:32] or "info"
    sender = clean(message.get("from"), "?", limit=64)
    return f"Moshi {message_id} — {kind} from {sender}"


def fetch_message(url: str, token: str, message_id: str, opener: Optional[Opener] = None,
                  timeout: float = 10) -> Dict[str, Any]:
    """`mesh_get` as the caller's agent, over moshi's stateless MCP endpoint. Never follows a redirect; errors
    never carry the token."""
    body = json.dumps({"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                       "params": {"name": "mesh_get", "arguments": {"message_id": message_id}}}).encode()
    request = urllib.request.Request(url, data=body, method="POST", headers={
        "Content-Type": "application/json", "Accept": "application/json, text/event-stream",
        "Authorization": f"Bearer {token}", "User-Agent": USER_AGENT})
    try:
        reply = send(opener or no_redirect_opener(), request, timeout)
    except HttpFailure as exc:
        raise _refusal(exc) from None
    result = _rpc_result(reply.body, reply.status)
    text = next((c.get("text") for c in result.get("content") or [] if isinstance(c, dict)), None)
    if result.get("isError") or not isinstance(text, str):
        raise ValueError(f"moshi does not know {message_id} (history is kept for 30 days)")
    message = parse_json(text.encode(), reply.status)
    if not isinstance(message, dict):
        raise ValueError("moshi answered mesh_get without a message")
    return message


def _rpc_result(body: bytes, status: int) -> Dict[str, Any]:
    text = body.decode("utf-8", errors="replace")
    if text.lstrip().startswith(("event:", "data:")):  # the event-stream form of one JSON-RPC answer
        text = "\n".join(line[5:].strip() for line in text.splitlines() if line.startswith("data:"))
    answer = parse_json(text.encode(), status)
    result = answer.get("result") if isinstance(answer, dict) else None
    if not isinstance(result, dict):
        raise ValueError("moshi refused mesh_get")
    return result


def _refusal(exc: HttpFailure) -> Exception:
    if exc.kind == "unreachable" or exc.status >= 500:
        return ValueError(UNREACHABLE)
    if exc.status == 401:
        return ValueError("moshi refused this agent's token")
    if exc.status == 409:
        return ValueError("this agent is active on another installation; moshi refuses its token here")
    return ValueError(f"moshi refused mesh_get ({exc.kind} {exc.status})")


def ring(paths: Paths, name: str, message_id: str, hc: Hc, environ: Mapping[str, str] = os.environ,
         fetch: Fetch = fetch_message) -> str:
    """Rings `name` for `message_id`; returns the terminal address it rang."""
    if not MESSAGE_ID.fullmatch(message_id or ""):
        raise ValueError("a message id looks like msg_ followed by 26 characters")
    config = load_config(paths)
    if not config.has("moshi"):
        raise ValueError("moshi is not set up; ring needs a message in moshi")
    token = environ.get(TOKEN_ENV["moshi"], "")
    if not token:
        raise ValueError(NO_IDENTITY)
    message = fetch(config.servers["moshi"], token, message_id)
    if str(message.get("to") or "").lower() != name.lower():
        raise ValueError(f"{message_id} is not addressed to {clean(name, '?', limit=64)}")
    target = _target(hc, name)
    hc.send(target.address, doorbell(message_id, message))
    return target.address


Probe = Callable[[str], Any]


def ring_without_record(paths: Paths, name: str, text: str, hc: Hc,
                        probe: Probe = probe_unauthenticated) -> str:
    """Pastes `[no-record] <text>` while moshi is down; refused while it answers."""
    line = " ".join(clean(text, "").split())[:MAX_TEXT]
    if not line:
        raise ValueError("--without-record needs the text to deliver")
    config = load_config(paths)
    if config.has("moshi") and _answers(probe, config.servers["moshi"]):
        raise ValueError("moshi is reachable: send the message through moshi, then ring with its id")
    target = _target(hc, name)
    hc.send(target.address, f"{NO_RECORD} {line}")
    return target.address


def _target(hc: Hc, name: str) -> roster_mod.Row:
    """The terminal that runs `name`, from hc alone: presence plays no part in where to ring."""
    here = roster_mod.build(hc.terminals(), {}, Mesh(), roster_mod.MOSHI_OFF, "")
    return roster_mod.running_agent(here, name)


def _answers(probe: Probe, url: str) -> bool:
    try:
        code, _ = probe(url)
    except McpError:
        return False
    return 0 < code < 500
