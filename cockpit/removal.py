"""`moshi-cockpit agent remove` and "Remove" in the dialog: the agent goes from this Mac.

This installation's token in moshi ends first (`drop_token`); the agent itself stays in moshi, other
installations keep theirs, and nothing is revoked with admin rights. Then its Keychain entries and its entry in
the agent list go, and the menu follows. When moshi cannot be told (not reachable, the token is dead, the
binding is wrong), the agent is removed here all the same, with one warning. No token is ever shown.
"""

from __future__ import annotations

from typing import List, Optional, Tuple

from cockpit import agents_remote, defaults, mesh_files, registry
from cockpit.agents import AgentContext
from cockpit.config import load_config
from cockpit.moshi_api import MoshiApi, MoshiError
from cockpit.paths import keychain_service
from cockpit.registry import Agent
from cockpit.secrets import KeychainError
from cockpit.text import clean
from cockpit.tokens import moshi_token

HERE_ONLY = "removed on this Mac only"


def remove_agent(ctx: AgentContext, name: str, api: Optional[MoshiApi] = None) -> Tuple[List[str], List[str]]:
    """(notes, warnings); never fails because of moshi."""
    target = registry.find(ctx.paths, name, enforce=False)
    notes: List[str] = []
    note, warning = _end_token(ctx, target, api)
    notes.extend([note] if note else [])
    for server in ("moshi", "ocellio"):
        service = keychain_service(server, target.name)
        if ctx.keychain.exists(service, target.name):
            ctx.keychain.delete(service, target.name)
            notes.append(f"Keychain entry {service} deleted")
    registry.remove(ctx.paths, ctx.backup, target.name)
    defaults.sync_agent_menu(ctx.paths, ctx.backup)
    mesh_files.remove_template(ctx.paths, target.name)
    notes.append(f"{target.name} removed from this Mac")
    return notes, [warning] if warning else []


def _end_token(ctx: AgentContext, agent: Agent, api: Optional[MoshiApi]) -> Tuple[Optional[str], Optional[str]]:
    """Ends this installation's token of the agent in moshi: (note, warning)."""
    config = load_config(ctx.paths)
    if not config.has("moshi") or not ctx.keychain.exists(keychain_service("moshi", agent.name), agent.name):
        return None, None
    url = config.servers["moshi"]
    try:
        token = moshi_token(ctx.paths, ctx.keychain, agent.name, url)  # only for the origin it is bound to
        (api or agents_remote.api_for(url)).drop_token(token)
    except MoshiError as exc:
        if exc.reason == "unreachable":
            return None, f"moshi is not reachable, so this Mac's token of {agent.name} was not ended there; {HERE_ONLY}"
        if exc.status == 401:
            return None, f"moshi no longer accepts this Mac's token of {agent.name}; {HERE_ONLY}"
        return None, f"moshi refused to end this Mac's token of {agent.name} ({exc.reason}); {HERE_ONLY}"
    except (ValueError, KeychainError) as exc:  # wrong binding, or the Keychain refused the read
        return None, f"The token of {agent.name} was not sent to moshi ({clean(str(exc), '?', limit=200)}); {HERE_ONLY}"
    return f"moshi: this Mac's token of {agent.name} ended; the agent stays in moshi", None
