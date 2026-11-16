"""`moshi-cockpit agent remove` and "Remove" in the dialog: the agent goes from this Mac.

Its Keychain entries and its entry in the agent list go, and the menu follows. Its moshi identity stays: this
Mac holds no token that may revoke identities, so the output says where to do that (the moshi dashboard). No
token is ever shown.
"""

from __future__ import annotations

from typing import List

from cockpit import defaults, registry
from cockpit.agents import AgentContext
from cockpit.config import load_config
from cockpit.harnesses import HARNESSES
from cockpit.paths import keychain_service


def remove_agent(ctx: AgentContext, name: str) -> List[str]:
    target = registry.find(ctx.paths, name, enforce=False)
    config = load_config(ctx.paths)
    notes: List[str] = []
    moshi_entry = (keychain_service("moshi", target.name), target.name)
    if config.has("moshi") and HARNESSES[target.harness].native_mesh and ctx.keychain.exists(*moshi_entry):
        notes.append(f"moshi identity '{target.name}' stays active: revoke it on the moshi dashboard if needed")
    for server in ("moshi", "ocellio"):
        service = keychain_service(server, target.name)
        if ctx.keychain.exists(service, target.name):
            ctx.keychain.delete(service, target.name)
            notes.append(f"Keychain entry {service} deleted")
    registry.remove(ctx.paths, ctx.backup, target.name)
    defaults.sync_agent_menu(ctx.paths, ctx.backup)
    notes.append(f"{target.name} removed from the agent list")
    return notes
