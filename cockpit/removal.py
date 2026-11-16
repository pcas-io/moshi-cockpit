"""`moshi-cockpit agent remove` and "Remove" in the dialog: the agent goes everywhere it is known.

Order: the moshi identity first (revoked through the admin API, after a question, only where an admin
token is stored and the agent holds its own moshi token here), then the agent's keychain entries, then the list entry. A failed revoke stops before
anything local is touched, so a second try finds everything as it was. Without admin access the moshi
identity stays active and the output says so (revoke it on the moshi dashboard). No token is ever shown.
"""

from __future__ import annotations

from typing import Callable, List, Optional

from cockpit import defaults, registry
from cockpit.admin import AdminApi, AdminError, has_admin_token
from cockpit.agents import AgentContext
from cockpit.config import load_config
from cockpit.harnesses import HARNESSES
from cockpit.paths import keychain_service
from cockpit.person import identity


def remove_agent(ctx: AgentContext, name: str, revoke: Optional[bool] = None,
                 confirm: Callable[[str], bool] = lambda _q: False,
                 admin_api: Optional[Callable[[], AdminApi]] = None) -> List[str]:
    """revoke=True/False decides without asking (panel, scripts); None asks through `confirm`."""
    target = registry.find(ctx.paths, name, enforce=False)
    config = load_config(ctx.paths)
    notes: List[str] = []
    if config.has("moshi") and HARNESSES[target.harness].native_mesh:
        moshi_name = identity(target.name, config.person)
        # Revoking goes by name, and moshi names are shared with everyone else in the mesh: only an agent
        # that holds a token of its own here is known to own that name.
        if not ctx.keychain.exists(keychain_service("moshi", target.name), target.name):
            notes.append(f"Kein eigener moshi-Eintrag für {target.name}: '{moshi_name}' wird nicht angefasst")
        elif not has_admin_token(ctx.keychain):
            notes.append(f"moshi-Identität '{moshi_name}' bleibt aktiv (kein Admin-Zugang hier): "
                         "bitte im moshi-Dashboard unter Agents widerrufen")
        elif revoke is False or (revoke is None and not confirm(f"moshi-Identität '{moshi_name}' widerrufen? [J/n] ")):
            notes.append(f"moshi-Identität '{moshi_name}' bleibt aktiv: bei Bedarf im moshi-Dashboard widerrufen")
        else:
            api = admin_api() if admin_api else AdminApi.from_keychain(ctx.keychain, config.server("moshi"), ctx.paths)
            try:
                api.revoke_agent(moshi_name)
                notes.append(f"moshi-Identität '{moshi_name}' widerrufen (bleibt im Audit, im Dashboard reaktivierbar)")
            except AdminError as exc:
                # Only moshi's own answer means the identity is gone. Any other 404 (a proxy, a wrong base path,
                # a moshi without this route) says nothing about it: it would stay active and orphaned.
                if exc.status != 404 or exc.reason != "not_found":
                    raise AdminError(f"{exc}. Nichts entfernt, noch einmal versuchen") from None
                notes.append(f"moshi-Identität '{moshi_name}' gibt es in moshi nicht (mehr)")
    for server in ("moshi", "ocellio"):
        service = keychain_service(server, target.name)
        if ctx.keychain.exists(service, target.name):
            ctx.keychain.delete(service, target.name)
            notes.append(f"Keychain-Eintrag {service} gelöscht")
    registry.remove(ctx.paths, ctx.backup, target.name)
    defaults.sync_agent_menu(ctx.paths, ctx.backup)
    notes.append(f"{target.name} aus der Agent-Liste genommen")
    return notes
