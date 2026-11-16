"""`moshi-cockpit connect`: the first sign-in of this Mac.

Herdr's first dialog opens this in a terminal tab, because a mod never sees a token. It asks, hidden and one
after the other, for a moshi registration token and the person's own ocellio token, and stores both in the
Keychain, bound to the origins of the configured servers. Whatever an earlier, interrupted run stored is not
asked for again.
"""

from __future__ import annotations

import getpass
import re
from dataclasses import dataclass
from typing import Callable, List, Tuple

from cockpit.agents import AgentContext
from cockpit.config import load_config, origin, require
from cockpit.paths import Paths, keychain_service
from cockpit.registry import load_agents
from cockpit.secrets import KeychainError, SecretStore, validate_token
from cockpit.tokens import OCELLIO_DEFAULT, REGISTER, bind_identity, bound_origin

DONE = 'Done. Back in Herdr, add your first agent with "+ Agent".'
KEPT = "What is stored already stays; a new attempt does not ask for it again."
REGISTER_ENTRY = (keychain_service("moshi", REGISTER), REGISTER)
OCELLIO_ENTRY = (keychain_service("ocellio", "default"), "default")
# moshi's format: rt_<ulid, lower case>_<32 random bytes, base64url>
REGISTER_TOKEN_RE = re.compile(r"rt_[0-9a-z]{26}_[A-Za-z0-9_-]{43}")


@dataclass(frozen=True)
class ConnectIO:
    ask_secret: Callable[[str], str] = getpass.getpass
    say: Callable[[str], None] = print


def needs_connect(paths: Paths, config, keychain: SecretStore) -> bool:
    """moshi is set up, but this Mac holds neither a registration token nor an agent's moshi token."""
    if not config.has("moshi"):
        return False
    if keychain.exists(*REGISTER_ENTRY):
        return False
    return not any(keychain.exists(keychain_service("moshi", a.name), a.name) for a in load_agents(paths, enforce=False))


def _missing(ctx: AgentContext, config) -> Tuple[bool, bool]:
    """Which tokens still have to be typed in."""
    moshi = not ctx.keychain.exists(*REGISTER_ENTRY)
    ocellio = config.has("ocellio") and not (
        bound_origin(ctx.paths, OCELLIO_DEFAULT) == origin(config.servers["ocellio"])
        and ctx.keychain.exists(*OCELLIO_ENTRY))
    return moshi, ocellio


def _intro(moshi: bool, ocellio: bool) -> str:
    if moshi and ocellio:
        return "You need two tokens: a moshi registration token (rt_…) and your own token from your ocellio account."
    return "You still need " + ("a moshi registration token (rt_…)." if moshi
                                else "your own token from your ocellio account.")


def worth_retrying(ctx: AgentContext) -> bool:
    """Whether "what is stored stays" is true and helps: not with nothing stored."""
    return ctx.keychain.exists(*REGISTER_ENTRY) or ctx.keychain.exists(*OCELLIO_ENTRY)


def _ask(io: ConnectIO, prompt: str, check: Callable[[str], None], what: str) -> str:
    for _ in range(3):
        token = io.ask_secret(prompt).strip()
        try:
            check(token)
            return token
        except KeychainError as exc:  # the message never contains the token
            io.say(f"   {exc}")
    raise ValueError(f"No valid {what} entered, cancelled")


def validate_register_token(token: str) -> None:
    if not REGISTER_TOKEN_RE.fullmatch(token or ""):
        raise KeychainError("That is not a registration token (format rt_…)")


def run(ctx: AgentContext, io: ConnectIO = ConnectIO()) -> List[str]:
    """Asks for what is missing and says each stored token at once: a later failure must not hide what is
    stored already. Returns what is left to say."""
    config = load_config(ctx.paths)
    url = require(config, "moshi")
    moshi, ocellio = _missing(ctx, config)
    if not (moshi or ocellio):
        return ["This Mac is connected already, nothing to do"]
    io.say(_intro(moshi, ocellio))
    if moshi:
        token = _ask(io, "moshi registration token (rt_…), input hidden: ", validate_register_token, "registration token")
        bind_identity(ctx.paths, ctx.backup, REGISTER, url)
        ctx.keychain.write(*REGISTER_ENTRY, token, strict=True)  # macOS asks on every read
        io.say(f"• {REGISTER_ENTRY[0]}: in the Keychain, bound to {origin(url)}")
    if ocellio:
        token = _ask(io, "Your ocellio token, input hidden: ", validate_token, "ocellio token")
        bind_identity(ctx.paths, ctx.backup, OCELLIO_DEFAULT, config.servers["ocellio"])
        ctx.keychain.write(*OCELLIO_ENTRY, token)
        io.say(f"• {OCELLIO_ENTRY[0]}: in the Keychain, bound to {origin(config.servers['ocellio'])}")
    return []
