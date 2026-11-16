"""`moshi-cockpit status`: the moshi mesh at a glance, read through one agent token from the keychain."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Callable, Dict, List, Optional, Sequence

from cockpit.keychain import Keychain, KeychainError
from cockpit.mcp import McpClient
from cockpit.paths import COCKPIT_IDENTITY, keychain_service
from cockpit.text import clean
from cockpit.tokens import moshi_token


def pick_identity(keychain: Keychain, requested: Optional[str], agent_names: Sequence[str] = ()) -> str:
    """A dedicated 'cockpit' identity is preferred: every MCP call refreshes the caller's presence,
    so reading with an agent's token would make that agent look online."""
    candidates = [requested] if requested else [COCKPIT_IDENTITY, *agent_names]
    for agent in candidates:
        if agent and keychain.exists(keychain_service("moshi", agent), agent):
            return agent
    raise KeychainError("Kein moshi-Token gefunden. Empfohlen: moshi-cockpit bot add cockpit")


def fetch_status(keychain: Keychain, identity: str, url: str, paths,
                 client_factory: Callable[[str, str], McpClient] = McpClient) -> Dict:
    token = moshi_token(paths, keychain, identity, url)  # only for the bound origin
    return client_factory(url, token).call_tool("mesh_status")


def fetch_agents(keychain: Keychain, identity: str, url: str, paths,
                 client_factory: Callable[[str, str], McpClient] = McpClient) -> List[Dict]:
    return list(fetch_status(keychain, identity, url, paths, client_factory).get("agents") or [])


FIELDS = ("name", "role", "presence", "working_on", "last_seen_at")


def local_agents(paths, config, keychain: Keychain) -> List[Dict]:
    """The person's registered agents for the sidebar: name, harness, moshi name, startable here."""
    from cockpit.harnesses import HARNESSES, Ctx
    from cockpit.registry import load_agents
    ctx = Ctx(paths=paths, config=config, keychain=keychain)
    out = []
    for agent in load_agents(paths, enforce=False):  # display: a broken rule is reported, not fatal
        harness = HARNESSES[agent.harness]
        out.append({"name": agent.name, "harness": agent.harness, "role": clean(agent.role, ""), "skill": agent.skill,
                    "moshi_name": harness.moshi_name(ctx, agent) if config.has("moshi") else "",
                    "installed": harness.installed(paths), "native": harness.native_mesh,
                    "connected": harness.native_mesh and bool(harness.servers(ctx)) and not harness.missing_tokens(ctx, agent),
                    "has_moshi_token": "moshi" in harness.servers(ctx)
                                       and keychain.exists(keychain_service("moshi", agent.name), agent.name)})
    return out


def problems(paths) -> List[str]:
    from cockpit.registry import load_agents, single_instance_problems
    return single_instance_problems(load_agents(paths, enforce=False))


def as_json(result: Dict, identity: str, local: Optional[List[Dict]] = None,
            servers: Optional[Dict[str, bool]] = None, harnesses: Optional[List[Dict]] = None,
            problems: Optional[List[str]] = None, admin: bool = False, needs_connect: bool = False) -> str:
    """Compact, sanitized view for the sidebar in herdr; no webhook or capability details. Without moshi
    `result` is empty and `identity` blank: the sidebar then shows the local agents without presence."""
    agents = [{f: clean(a.get(f), "") for f in FIELDS} for a in result.get("agents") or []]
    payload = {"identity": identity,
               "agents": agents, "local": list(local or []), "servers": dict(servers or {}),
               "harnesses": list(harnesses or []), "problems": list(problems or []),
               "admin": bool(admin), "needs_connect": bool(needs_connect)}
    return json.dumps(payload, ensure_ascii=False)


def render(agents: List[Dict], identity: str) -> str:
    order = {"live": 0, "stale": 1, "offline": 2, "never": 3}
    rows = sorted(agents, key=lambda a: (order.get(a.get("presence") or "never", 9), clean(a.get("name"), "").lower()))
    marks = {"live": "●", "stale": "◐", "offline": "○", "never": "·"}
    lines = [f"{'':2}{'Agent':<17}{'Rolle':<18}{'zuletzt':<18}working_on"]
    for agent in rows:
        presence = agent.get("presence") or "never"
        seen = clean(agent.get("last_seen_at"))[:16].replace("T", " ")
        lines.append(f"{marks.get(presence, '?'):<2}{clean(agent.get('name'), '?')[:16]:<17}"
                     f"{clean(agent.get('role'))[:17]:<18}{seen:<18}{clean(agent.get('working_on'))[:90]}")
    live = sum((a.get("presence") == "live") for a in agents)
    lines.append(f"\n{len(agents)} Agents · {live} live · gelesen als '{identity}'")
    if identity != COCKPIT_IDENTITY:
        lines.append(f"Hinweis: Der Aufruf frischt die Präsenz von '{identity}' auf. "
                     "Mit einer eigenen Identität: moshi-cockpit bot add cockpit")
    return "\n".join(lines)
