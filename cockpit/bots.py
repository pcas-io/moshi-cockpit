"""`moshi-cockpit bot add <agent>`: onboarding of a moshi identity.

The moshi identity is created, in this order:
1. with the registration token (`moshi-cockpit bot register-token set`): it can create agents
   with names under its prefix and nothing else. A refusal ends the run; there is no fallback to the
   admin token, so a narrow token never silently escalates to the broad one.
2. with the admin token (`moshi-cockpit bot admin-token`), on admin machines.
3. on the connect page: the human creates it and pastes the token into a hidden prompt.
   --browser always takes this way.
In 1 and 2 the new token goes straight into the keychain: no browser, nothing shown.
"""

from __future__ import annotations

import getpass
import webbrowser
from dataclasses import dataclass
from typing import Callable, List, Optional, Tuple

from cockpit.admin import AdminApi, has_admin_token
from cockpit.register import RegisterApi, has_register_token
from cockpit.agents import AgentContext, harness_ctx, setup
from cockpit.config import load_config, moshi_base, origin, require
from cockpit.harnesses import HARNESSES
from cockpit.secrets import KeychainError, validate_token
from cockpit.paths import COCKPIT_IDENTITY, keychain_service
from cockpit.registry import bind, find, load_agents
from cockpit.tokens import COCKPIT_OCELLIO, bind_identity, bound_origin
from cockpit.text import clean



@dataclass(frozen=True)
class BotIO:
    ask_secret: Callable[[str], str] = getpass.getpass
    open_url: Callable[[str], bool] = webbrowser.open
    say: Callable[[str], None] = print
    # Build the API clients (each reads its token: one keychain dialog). Injected by tests.
    admin_api: Optional[Callable[[], AdminApi]] = None
    register_api: Optional[Callable[[], RegisterApi]] = None


def add(agent: str, ctx: AgentContext, io: BotIO = BotIO(), strict: bool = False, browser: bool = False,
        rebind: bool = False, auto: bool = False) -> List[str]:
    """Connects a registered agent (or the cockpit identity) to the configured servers: moshi identity
    and ocellio token into the keychain, then the agent's files. Unset servers are skipped.

    auto=True never asks a person: moshi through the registration or admin token, ocellio as a copy of the
    person's token another agent here already holds for the same server (person model: every agent
    has a copy). If that is not possible it raises before anything was created."""
    config = load_config(ctx.paths)
    if agent == COCKPIT_IDENTITY:
        url = require(config, "moshi")
        bound = bind_identity(ctx.paths, ctx.backup, COCKPIT_IDENTITY, url, rebind=rebind)
        return [_store_moshi(agent, ctx, io, strict, browser, config), f"cockpit: Token gebunden an {bound}"]
    registered = find(ctx.paths, agent)
    agent = registered.name
    notes = []
    harness = HARNESSES[registered.harness]
    servers = harness.servers(harness_ctx(ctx))
    ocellio_copy = None
    if auto:  # everything that can stop an automatic run, before moshi creates anything
        if "moshi" in servers and not ctx.keychain.exists(keychain_service("moshi", agent), agent) \
                and not (has_register_token(ctx.keychain) or has_admin_token(ctx.keychain)):
            raise ValueError("Kein Registrier- oder Admin-Token hier, moshi kann nicht automatisch anlegen")
        ocellio_service = keychain_service("ocellio", agent)
        if "ocellio" in servers and not ctx.keychain.restore_parked(ocellio_service, agent) \
                and not ctx.keychain.exists(ocellio_service, agent):
            ocellio_copy = _person_ocellio_token(agent, ctx, config)
            if ocellio_copy is None:
                raise ValueError("Noch kein ocellio-Token auf diesem Rechner, das der Agent übernehmen könnte")
    if "moshi" in servers:
        notes.append(_store_moshi(agent, ctx, io, strict, browser, config))
    if "ocellio" in servers:
        if ocellio_copy is not None:
            token, source_strict = ocellio_copy
            ctx.keychain.write(keychain_service("ocellio", agent), agent, token, strict=strict or source_strict)
            notes.append(f"{keychain_service('ocellio', agent)}: dein ocellio-Token übernommen (nie angezeigt)")
        else:
            notes.append(_store_ocellio(agent, ctx, io, strict))
    if not servers:
        notes.append(f"{agent}: {registered.harness} hat noch keine eigene Anbindung, nichts einzurichten")
    origins = {s: origin(config.servers[s]) for s in servers}
    if origins:
        registered = bind(ctx.paths, ctx.backup, agent, origins, rebind=rebind)
        notes.append(f"{agent}: Tokens gebunden an " + ", ".join(f"{s} {o}" for s, o in origins.items()))
    changed = setup(ctx, registered)
    notes.append(f"{agent} eingerichtet" + (f": {', '.join(changed)}" if changed else " (keine Änderung)"))
    return notes


def _existing(service: str, agent: str, ctx: AgentContext, strict: bool) -> Optional[str]:
    """None when the entry is missing. With --strict-keychain an existing entry is hardened, not skipped:
    its value is read once (no dialog while it is not strict yet) and written back strict, then checked."""
    if ctx.keychain.restore_parked(service, agent):
        return f"{service}: aus der Sicherung nach einem abgebrochenen Härten wiederhergestellt (gehärtet)"
    if not ctx.keychain.exists(service, agent):
        return None
    if not strict:
        return f"{service}: schon in der Keychain, unverändert"
    if ctx.keychain.is_strict(service, agent):
        return f"{service}: schon in der Keychain und gehärtet"
    ctx.keychain.write(service, agent, ctx.keychain.read(service, agent), strict=True)
    return f"{service}: nachträglich gehärtet (macOS fragt ab jetzt bei jedem Lesen)"


def _store_moshi(agent: str, ctx: AgentContext, io: BotIO, strict: bool, browser: bool, config) -> str:
    service = keychain_service("moshi", agent)
    existing = _existing(service, agent, ctx, strict)
    if existing:
        return existing
    if not browser:
        url, name = config.server("moshi"), agent
        # A registration token, if there is one, is the only API way: its refusal ends the run, the admin
        # token is never tried after it, so a narrow token never escalates to the broad one.
        if has_register_token(ctx.keychain):
            way = "Registrier-Token"
            build = io.register_api or (lambda: RegisterApi.from_keychain(ctx.keychain, url, ctx.paths))
        elif has_admin_token(ctx.keychain):
            way = "Admin-Token"
            build = io.admin_api or (lambda: AdminApi.from_keychain(ctx.keychain, url, ctx.paths))
        else:
            build = None
        if build is not None:
            io.say(f"   moshi-Identität '{name}' wird mit dem {way} angelegt (macOS fragt nach dem Schlüsselbund)")
            created, token = build().create_agent(name)
            try:
                ctx.keychain.write(service, agent, token, strict=strict)
            except KeychainError as exc:
                # The identity exists in moshi now, but its token is gone with this process.
                raise ValueError(f"{exc}. Die Identität '{agent}' (id {clean(created.get('id'), '?')}) ist in moshi "
                                 "angelegt, ihr Token aber verloren. Im moshi-Dashboard unter Agents das Token "
                                 f"zurücksetzen, dann: moshi-cockpit bot add {agent} --browser") from None
            return f"{service}: Identität per {way} angelegt, Token in der Keychain (nie angezeigt)"
    connect = moshi_base(config.server("moshi")) + "/agents/connect"
    io.say(f"1. In moshi die Identität '{agent}' anlegen: {connect}")
    io.say("   (Name eingeben, Token kopieren. Das Token wird nur einmal angezeigt.)")
    io.open_url(connect)
    token = _ask_token(io, f"2. moshi-Token für '{agent}' einfügen (Eingabe unsichtbar): ")
    ctx.keychain.write(service, agent, token, strict=strict)
    return f"{service}: in der Keychain gespeichert"


def _person_ocellio_token(agent: str, ctx: AgentContext, config) -> Optional[Tuple[str, bool]]:
    """The person's ocellio token as another agent holds it, only if bound to the configured server, and
    whether that entry is hardened (-T ""): a copy never gets weaker protection than its source."""
    wanted = origin(config.servers["ocellio"])
    for other in load_agents(ctx.paths, enforce=False):
        service = keychain_service("ocellio", other.name)
        if other.name != agent and other.bound.get("ocellio") == wanted and ctx.keychain.exists(service, other.name):
            return ctx.keychain.read(service, other.name), ctx.keychain.is_strict(service, other.name)
    service = keychain_service("ocellio", COCKPIT_IDENTITY)  # from `moshi-cockpit connect`, before the first agent
    if bound_origin(ctx.paths, COCKPIT_OCELLIO) == wanted and ctx.keychain.exists(service, COCKPIT_IDENTITY):
        return ctx.keychain.read(service, COCKPIT_IDENTITY), ctx.keychain.is_strict(service, COCKPIT_IDENTITY)
    return None


def _store_ocellio(agent: str, ctx: AgentContext, io: BotIO, strict: bool) -> str:
    service = keychain_service("ocellio", agent)
    existing = _existing(service, agent, ctx, strict)
    if existing:
        return existing
    token = _ask_token(io, f"3. ocellio-Token für '{agent}' einfügen (Eingabe unsichtbar): ")
    ctx.keychain.write(service, agent, token, strict=strict)
    return f"{service}: in der Keychain gespeichert"


def ask_token(io: BotIO, prompt: str) -> str:
    return _ask_token(io, prompt)


def _ask_token(io: BotIO, prompt: str) -> str:
    for _ in range(3):
        token = io.ask_secret(prompt).strip()
        try:
            validate_token(token)
            return token
        except KeychainError as exc:  # the message never contains the token
            io.say(f"   {exc}")
    raise ValueError("Kein gültiges Token eingegeben, abgebrochen")
