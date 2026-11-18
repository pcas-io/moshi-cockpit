"""`moshi-cockpit status` and `agent where`: the mesh as this installation sees it, read with the device key.

The device key only reads moshi's installation status. No MCP call is made, so reading changes no agent's
presence. A device key moshi refuses (removed in the dashboard) makes the view `disconnected`, no usable
answer makes it `unreachable`; neither is an error for the sidebar.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from cockpit import agents_remote
from cockpit.agents_remote import APPROVAL, ApprovalRequired
from cockpit.config import Config, origin, require
from cockpit.connect import DEVICE_ENTRY
from cockpit.moshi_api import MoshiApi, MoshiError
from cockpit.paths import keychain_service
from cockpit.secrets import SecretStore
from cockpit.text import clean
from cockpit.tokens import DEVICE, bound_origin, moshi_token

FIELDS = ("name", "role", "presence", "working_on", "last_seen_at", "kind", "harness", "skill")


@dataclass(frozen=True)
class Mesh:
    installation: Optional[Dict[str, str]] = None  # this Herdr in moshi: {id, label}
    agents: List[Dict[str, Any]] = field(default_factory=list)
    disconnected: bool = False
    unreachable: bool = False


def unreachable(exc: MoshiError) -> bool:
    """No usable answer: no connection, a broken one, or a server error."""
    return exc.reason in ("unreachable", "bad_response") or exc.status >= 500


def mesh_from(body: Dict[str, Any]) -> Mesh:
    """moshi's installation status, sanitized: text only, no control characters, null where nothing is set."""
    found = body.get("installation")
    installation = {key: clean(found.get(key), "") for key in ("id", "label")} if isinstance(found, dict) else None
    agents = []
    for agent in body.get("agents") or []:
        if isinstance(agent, dict):
            active = agent.get("active_installation")
            row: Dict[str, Any] = {key: clean(agent.get(key), "") for key in FIELDS}
            row["active_installation"] = clean(active) if isinstance(active, str) and active else None
            agents.append(row)
    return Mesh(installation=installation, agents=agents)


def device_key(paths, config: Config, keychain: SecretStore) -> Optional[str]:
    """The device key, when this Herdr holds one bound to the configured moshi (never sent elsewhere)."""
    url = config.servers["moshi"]
    if not keychain.exists(*DEVICE_ENTRY) or bound_origin(paths, DEVICE) != origin(url):
        return None
    return moshi_token(paths, keychain, DEVICE, url)


def read_mesh(paths, config: Config, keychain: SecretStore, api: Optional[MoshiApi] = None) -> Mesh:
    """Empty without moshi or before connect; a device key of no server or of another one counts as
    disconnected, since it cannot be sent."""
    if not config.has("moshi"):
        return Mesh()
    key = device_key(paths, config, keychain)
    if key is None:
        return Mesh(disconnected=keychain.exists(*DEVICE_ENTRY))
    try:
        body = (api or agents_remote.api_for(config.servers["moshi"])).installation_status(key)
    except MoshiError as exc:
        if exc.status in (401, 403):
            return Mesh(disconnected=True)
        if unreachable(exc):
            return Mesh(unreachable=True)
        raise
    return mesh_from(body)


def where(paths, config: Config, keychain: SecretStore, name: str, api: Optional[MoshiApi] = None) -> Dict[str, Any]:
    """`{"active": label | None, "here": bool, "own": label | None}`: the installation that holds the agent,
    whether that is this one, and this installation's label. moshi names installations by label only, so two with the same label look alike. Raises
    ApprovalRequired without a usable device key, MoshiError when moshi gives no answer."""
    url = require(config, "moshi")
    key = device_key(paths, config, keychain)
    if key is None:
        raise ApprovalRequired(APPROVAL)
    try:
        mesh = mesh_from((api or agents_remote.api_for(url)).installation_status(key))
    except MoshiError as exc:
        if exc.status in (401, 403):
            raise ApprovalRequired(APPROVAL) from None
        raise
    found = next((a for a in mesh.agents if a["name"].lower() == name.lower()), None)
    active = found["active_installation"] if found else None
    own = (mesh.installation or {}).get("label")
    return {"active": active, "here": active is not None and active == own, "own": own}


def local_agents(paths, config, keychain: SecretStore) -> List[Dict]:
    """The person's registered agents for the sidebar: name, harness, startable here. An agent's moshi name
    is its name."""
    from cockpit.harnesses import HARNESSES, Ctx
    from cockpit.registry import load_agents
    ctx = Ctx(paths=paths, config=config, keychain=keychain)
    out = []
    for agent in load_agents(paths, enforce=False):  # display: a broken rule is reported, not fatal
        harness = HARNESSES[agent.harness]
        out.append({"name": agent.name, "harness": agent.harness, "role": clean(agent.role, ""), "skill": agent.skill,
                    "installed": harness.installed(paths), "native": harness.native_mesh,
                    "connected": harness.native_mesh and bool(harness.servers(ctx)) and not harness.missing_tokens(ctx, agent),
                    "has_moshi_token": "moshi" in harness.servers(ctx)
                                       and keychain.exists(keychain_service("moshi", agent.name), agent.name)})
    return out


def problems(paths) -> List[str]:
    from cockpit.registry import load_agents, single_instance_problems
    return single_instance_problems(load_agents(paths, enforce=False))


def as_json(mesh: Mesh, local: Optional[List[Dict]] = None, servers: Optional[Dict[str, bool]] = None,
            harnesses: Optional[List[Dict]] = None, problems: Optional[List[str]] = None,
            needs_connect: bool = False, approval_valid: bool = False) -> str:
    """Compact view for the sidebar in herdr; no webhook or capability details. Without moshi the mesh is
    empty: the sidebar then shows the local agents without presence."""
    payload = {"agents": list(mesh.agents), "local": list(local or []), "installation": mesh.installation,
               "servers": dict(servers or {}), "harnesses": list(harnesses or []), "problems": list(problems or []),
               "needs_connect": bool(needs_connect), "approval_valid": bool(approval_valid),
               "disconnected": mesh.disconnected, "unreachable": mesh.unreachable}
    return json.dumps(payload, ensure_ascii=False)


def render(mesh: Mesh) -> str:
    if mesh.disconnected:
        return "Disconnected from moshi: run moshi-cockpit connect."
    if mesh.unreachable:
        return "moshi is not reachable. Check the network and try again."
    order = {"live": 0, "stale": 1, "offline": 2, "never": 3}
    rows = sorted(mesh.agents, key=lambda a: (order.get(a["presence"] or "never", 9), a["name"].lower()))
    marks = {"live": "●", "stale": "◐", "offline": "○", "never": "·"}
    lines = [f"{'':2}{'Agent':<17}{'Role':<18}{'Last seen':<18}{'Active on':<17}working_on"]
    for agent in rows:
        presence = agent["presence"] or "never"
        seen = agent["last_seen_at"][:16].replace("T", " ")
        lines.append(f"{marks.get(presence, '?'):<2}{(agent['name'] or '?')[:16]:<17}{agent['role'][:17]:<18}"
                     f"{seen:<18}{(agent['active_installation'] or '')[:16]:<17}{agent['working_on'][:90]}")
    live = sum(a["presence"] == "live" for a in mesh.agents)
    label = (mesh.installation or {}).get("label") or "?"
    lines.append(f"\n{len(mesh.agents)} agents · {live} live · this Herdr is '{label}' in moshi")
    return "\n".join(lines)
