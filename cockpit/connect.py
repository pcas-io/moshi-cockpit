"""`moshi-cockpit connect`: the first sign-in of this Mac.

herdr's first dialog opens this in a terminal tab, because a mod never sees a token. It asks for the moshi
registration token and the person's ocellio token, hidden and one after the other, and creates the cockpit
identity. After that every "+ Agent" connects by itself: moshi through the registration token, ocellio as a
copy of the person's token (the agents act with the person's own rights there).
"""

from __future__ import annotations

import getpass
from dataclasses import dataclass
from typing import Callable, List, Tuple

from cockpit import admin, bots, register
from cockpit.agents import AgentContext
from cockpit.config import load_config, origin, require
from cockpit.keychain import Keychain, KeychainError
from cockpit.paths import COCKPIT_IDENTITY, Paths, keychain_service
from cockpit.person import identity
from cockpit.registry import load_agents
from cockpit.tokens import COCKPIT_OCELLIO, REGISTER, bind_identity, bound_origin

DONE = "Fertig. Zurück in Herdr legst du unter Your agents mit „+ Agent“ deinen ersten Agent an."
KEPT = "Schon Gespeichertes bleibt, ein neuer Versuch fragt es nicht noch einmal ab."


@dataclass(frozen=True)
class ConnectIO:
    ask_secret: Callable[[str], str] = getpass.getpass
    say: Callable[[str], None] = print


def needs_connect(paths: Paths, config, keychain: Keychain) -> bool:
    """moshi is set up, but no identity on this Mac can read it yet."""
    if not config.has("moshi"):
        return False
    names = [COCKPIT_IDENTITY, *(a.name for a in load_agents(paths, enforce=False))]
    return not any(keychain.exists(keychain_service("moshi", n), n) for n in names)


def _missing(ctx: AgentContext, config) -> Tuple[bool, bool]:
    """Which tokens still have to be typed in. Whatever a first, interrupted run stored is not asked again."""
    cockpit = keychain_service("moshi", COCKPIT_IDENTITY)
    moshi = not (register.has_register_token(ctx.keychain) or admin.has_admin_token(ctx.keychain)
                 or ctx.keychain.exists(cockpit, COCKPIT_IDENTITY))
    ocellio = config.has("ocellio") and not (
        bound_origin(ctx.paths, COCKPIT_OCELLIO) == origin(config.servers["ocellio"])
        and ctx.keychain.exists(keychain_service("ocellio", COCKPIT_IDENTITY), COCKPIT_IDENTITY))
    return moshi, ocellio


def _intro(moshi: bool, ocellio: bool) -> str:
    if moshi and ocellio:
        return ("Du brauchst zwei Tokens: das moshi-Registrier-Token von deinem Admin und dein eigenes ocellio-Token "
                "aus deinem ocellio-Konto.")
    return "Du brauchst noch " + ("das moshi-Registrier-Token von deinem Admin." if moshi
                                  else "dein eigenes ocellio-Token aus deinem ocellio-Konto.")


def taken_message(name: str) -> str:
    return (f"Die Identität '{name}' gibt es in moshi schon, vermutlich von einem anderen Mac. Setz ihr Token nicht "
            "zurück, das würde den anderen Mac trennen. Hol dir vom Admin ein Registrier-Token für einen eigenen "
            "Personen-Namen, dann: moshi-cockpit bot register-token remove, moshi-cockpit install --person <name> "
            "und noch einmal verbinden.")


def worth_retrying(ctx: AgentContext, exc: Exception) -> bool:
    """Whether "what is stored stays" is true and helps: not with nothing stored, not for a name that is taken."""
    if getattr(exc, "reason", "") == "name_taken":
        return False
    return register.has_register_token(ctx.keychain) \
        or ctx.keychain.exists(keychain_service("ocellio", COCKPIT_IDENTITY), COCKPIT_IDENTITY)


def _ask_register_token(io: bots.BotIO) -> str:
    for _ in range(3):
        token = io.ask_secret("moshi-Registrier-Token (rt_…) einfügen (Eingabe unsichtbar): ").strip()
        try:
            register.validate_register_token(token)
            return token
        except KeychainError as exc:  # the message never contains the token
            io.say(f"   {exc}")
    raise ValueError("Kein gültiges Registrier-Token eingegeben, abgebrochen")


def run(ctx: AgentContext, io: ConnectIO = ConnectIO()) -> List[str]:
    config = load_config(ctx.paths)
    url = require(config, "moshi")
    bot_io = bots.BotIO(ask_secret=io.ask_secret, say=io.say)
    moshi, ocellio = _missing(ctx, config)
    if moshi or ocellio:
        io.say(_intro(moshi, ocellio))
    if moshi:  # said at once: a later failure must not hide what is stored already
        token = _ask_register_token(bot_io)
        bind_identity(ctx.paths, ctx.backup, REGISTER, url)
        register.store_register_token(ctx.keychain, token)
        io.say(f"• {register.REGISTER_SERVICE}: in der Keychain")
    if ocellio:
        token = bots.ask_token(bot_io, "ocellio-Token einfügen (Eingabe unsichtbar): ")
        bind_identity(ctx.paths, ctx.backup, COCKPIT_OCELLIO, config.servers["ocellio"])
        ctx.keychain.write(keychain_service("ocellio", COCKPIT_IDENTITY), COCKPIT_IDENTITY, token)
        io.say(f"• {keychain_service('ocellio', COCKPIT_IDENTITY)}: in der Keychain")
    try:
        return bots.add(COCKPIT_IDENTITY, ctx, bot_io)
    except admin.AdminError as exc:
        if exc.reason != "name_taken":
            raise
        # moshi's general advice is to reset the token: for this name that would cut off the other Mac.
        raise admin.AdminError(taken_message(identity(COCKPIT_IDENTITY, config.person)), exc.reason, exc.status) from None
