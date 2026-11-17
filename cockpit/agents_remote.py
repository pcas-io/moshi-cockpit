"""Agents in moshi: `agent add`, `agent load`, `agent catalog`, `agent set` (and `agent remove`, see removal.py).

One token per agent and installation: creating or loading an agent here gets this installation a token of its
own (Keychain `moshi-<agent>`), and the person's ocellio token is copied to `ocellio-<agent>`. Creating and
loading need the registration token from `connect`; while it is missing, expired or refused, they end with
ApprovalRequired (exit code 3: the panel then opens the connect dialog). Every local check runs before moshi
is asked. No token is ever printed.
"""

from __future__ import annotations

from dataclasses import replace
from typing import Any, Dict, List, Optional, Tuple

from cockpit import agents, defaults, registry
from cockpit.agents import AgentContext
from cockpit.config import Config, load_config, origin, require
from cockpit.connect import DEVICE_ENTRY, OCELLIO_ENTRY, approval_valid
from cockpit.harnesses import HARNESSES, Ctx
from cockpit.moshi_api import MoshiApi, MoshiError
from cockpit.paths import keychain_service
from cockpit.registry import MAX_ROLE, NAME_RE, Agent, load_agents, validate_name
from cockpit.secrets import KeychainError, validate_token
from cockpit.text import clean
from cockpit.tokens import DEVICE, OCELLIO_DEFAULT, REGISTER, bound_origin, moshi_token

APPROVAL = "This Herdr needs a new approval from moshi: run moshi-cockpit connect."
NAME_TAKEN = "That name is taken in moshi."
UNREACHABLE = "Could not reach moshi. Check the network and try again."
PROFILE = "moshi refused the role or skill (role: one line, at most 64 characters; skill: letters, digits, _ and -)."
# One text for the CLI and the panel (`run`, `agent set`).
DEAD_TOKEN = ('moshi no longer accepts the token of {name} on this Mac: remove the agent here and load it again '
              '(moshi-cockpit agent remove {name}, then moshi-cockpit agent load {name}, or "+ Agent" in Herdr).')


class ApprovalRequired(RuntimeError):
    """The registration token (or the device key) is missing, expired or refused: exit code 3."""


def api_for(url: str) -> MoshiApi:
    """The client for the configured moshi (tests replace it)."""
    return MoshiApi(url)


def refusal(exc: MoshiError, name: str = "") -> ValueError:
    """One sentence for a refusal or failure of moshi; never a token (MoshiError carries none)."""
    reasons = {"unreachable": UNREACHABLE, "name_taken": NAME_TAKEN, "invalid_profile": PROFILE,
               "not_found": f"moshi has no agent called {name}.",
               "not_local": f"{name} is not a local agent in moshi, so it cannot run here.",
               "invalid_name": "moshi refused the name (letters, digits, _ and -, starting with a letter or digit, "
                               "at most 64 characters)."}
    if exc.reason in reasons:
        return ValueError(reasons[exc.reason])
    if exc.status == 429:
        return ValueError("moshi allows only a few new agents per hour. Try again later.")
    if exc.status >= 500:
        return ValueError(f"moshi answered with an error ({exc.status}). Try again later.")
    return ValueError(f"moshi refused this ({exc.reason}, HTTP {exc.status}).")


def check_harness(ctx: AgentContext, harness: str) -> None:
    found = HARNESSES.get(harness)
    if found is None:
        raise ValueError(f"Unknown harness '{harness[:40]}'. Allowed: {', '.join(HARNESSES)}")
    if found.no_own_agents:
        raise ValueError(f"{harness} {found.no_own_agents}")
    if found.single_instance:
        taken = [a.name for a in load_agents(ctx.paths) if a.harness == harness]
        if taken:
            raise ValueError(f"{harness} {found.single_reason}: there is already the agent '{taken[0]}'. "
                             f"First: moshi-cockpit agent remove {taken[0]}")


def notes(ctx: AgentContext, agent: Agent) -> List[str]:
    """What still stands between the agent and its first start, one line each."""
    harness = HARNESSES[agent.harness]
    found = []
    if not harness.installed(ctx.paths):
        found.append(f"{harness.binary} is not installed here: install it to start {agent.name}")
    missing = harness.missing_tokens(agents.harness_ctx(ctx), agent)
    if missing:
        found.append(f"Not connected to every server yet ({', '.join(missing)}): {agents.CONNECT_HINT}")
    return found


# -- create and load ------------------------------------------------------------------------------


def create(ctx: AgentContext, name: str, harness: str, role: str = "", skill: str = "",
           api: Optional[MoshiApi] = None) -> Agent:
    """A new agent here and, with moshi set up, in moshi, held by this installation."""
    check_harness(ctx, harness)
    draft = registry.check_new(ctx.paths, Agent(name=validate_name(name), harness=harness, role=role, skill=skill))
    config = load_config(ctx.paths)
    if "moshi" not in _servers(ctx, config, harness):
        registry.add(ctx.paths, ctx.backup, draft)
        return _finish(ctx, draft)
    url = config.servers["moshi"]
    registration = _registration_token(ctx, url)
    api = api or api_for(url)
    try:
        _, token = api.register_agent(registration, draft.name, harness, draft.role or None, draft.skill or None)
    except MoshiError as exc:
        raise _registration_refusal(exc, draft.name) from None
    try:
        agent = _store(ctx, config, draft, token)
    except (ValueError, KeychainError, OSError) as exc:  # the agent exists in moshi now: it can be loaded later
        _discard(api, token)
        kind = ValueError if isinstance(exc, OSError) else type(exc)
        raise kind(f"{_failure_text(exc).rstrip('.')}. Load it with: moshi-cockpit agent load {draft.name}") from None
    return _finish(ctx, agent)


def load(ctx: AgentContext, name: str, api: Optional[MoshiApi] = None) -> Agent:
    """An agent that exists in moshi, here as well: harness, role and skill come from moshi. A harness that
    is not installed here is fine (the panel offers to install it). Loading takes nothing over."""
    name = validate_name(name)
    if any(a.name.lower() == name.lower() for a in load_agents(ctx.paths, enforce=False)):
        raise ValueError(f"There is already an agent '{name}' here")
    config = load_config(ctx.paths)
    url = require(config, "moshi")
    registration = _registration_token(ctx, url)
    api = api or api_for(url)
    try:
        profile, token = api.load_agent(registration, name)
    except MoshiError as exc:
        raise _registration_refusal(exc, name) from None
    try:
        agent = _store(ctx, config, _from_profile(ctx, profile, name), token)
    except (ValueError, KeychainError, OSError):
        _discard(api, token)
        raise
    return _finish(ctx, agent)


def _servers(ctx: AgentContext, config: Config, harness: str) -> List[str]:
    return HARNESSES[harness].servers(Ctx(paths=ctx.paths, config=config, keychain=ctx.keychain))


def _registration_token(ctx: AgentContext, url: str) -> str:
    """Read only while the approval is valid and bound to this moshi (macOS asks on every read)."""
    if not approval_valid(ctx.paths, ctx.keychain) or bound_origin(ctx.paths, REGISTER) != origin(url):
        raise ApprovalRequired(APPROVAL)
    return moshi_token(ctx.paths, ctx.keychain, REGISTER, url)


def _registration_refusal(exc: MoshiError, name: str) -> Exception:
    """Any 401 on /api/register (invalid_token, token_expired, token_revoked, ...) asks for a new approval."""
    return ApprovalRequired(APPROVAL) if exc.status == 401 else refusal(exc, name)


def _from_profile(ctx: AgentContext, profile: Dict[str, Any], name: str) -> Agent:
    remote = profile.get("name")
    if not isinstance(remote, str) or remote.lower() != name.lower():
        raise ValueError(f"moshi answered for another agent than {name}.")
    harness = profile.get("harness")
    if not isinstance(harness, str) or harness not in HARNESSES:
        shown = clean(harness if isinstance(harness, str) else "", "no harness", limit=40)
        raise ValueError(f"{name} runs on {shown} in moshi, which this version cannot start.")
    check_harness(ctx, harness)
    role, skill = profile.get("role"), profile.get("skill")
    return registry.check_new(ctx.paths, Agent(name=remote, harness=harness, role=role if isinstance(role, str) else "",
                                               skill=skill if isinstance(skill, str) else ""))


def _store(ctx: AgentContext, config: Config, draft: Agent, token: str) -> Agent:
    """The agent's tokens into the Keychain, then its entry, bound to the origins they are for. On a
    failure the entries written here go again: no token stays behind without its agent."""
    validate_token(token)
    written = []
    try:
        ctx.keychain.write(keychain_service("moshi", draft.name), draft.name, token)
        written.append("moshi")
        bound = {"moshi": origin(config.servers["moshi"])}
        if "ocellio" in _servers(ctx, config, draft.harness) and _copy_ocellio(ctx, config, draft.name):
            written.append("ocellio")
            bound["ocellio"] = origin(config.servers["ocellio"])
        agent = replace(draft, bound=bound)
        registry.add(ctx.paths, ctx.backup, agent)
    except (ValueError, KeychainError, OSError):
        for server in written:
            ctx.keychain.delete(keychain_service(server, draft.name), draft.name)
        raise
    return agent


def _copy_ocellio(ctx: AgentContext, config: Config, name: str) -> bool:
    """The person's ocellio token becomes the agent's, only for the server it was given for."""
    if not ctx.keychain.exists(*OCELLIO_ENTRY) or bound_origin(ctx.paths, OCELLIO_DEFAULT) != origin(config.servers["ocellio"]):
        return False
    ctx.keychain.write(keychain_service("ocellio", name), name, ctx.keychain.read(*OCELLIO_ENTRY))
    return True


def _failure_text(exc: Exception) -> str:
    """One readable sentence: a file error as "<reason>: <path>", not "[Errno 13] …"."""
    if isinstance(exc, OSError) and exc.strerror:
        return f"{exc.strerror}: {exc.filename}" if exc.filename else exc.strerror
    return str(exc)


def _discard(api: MoshiApi, token: str) -> None:
    """Best effort: a token that was never stored ends at once. If moshi cannot be told, nobody holds it."""
    try:
        api.drop_token(token)
    except MoshiError:
        pass


def _finish(ctx: AgentContext, agent: Agent) -> Agent:
    defaults.sync_agent_menu(ctx.paths, ctx.backup)
    if not HARNESSES[agent.harness].missing_tokens(agents.harness_ctx(ctx), agent):
        agents.setup(ctx, agent)  # vibe and agy keep their rules in a file
    return agent


# -- catalog, set, remove -------------------------------------------------------------------------


def catalog(ctx: AgentContext, api: Optional[MoshiApi] = None) -> List[Dict[str, Any]]:
    """moshi's local agents that are not here yet, read with the device key, sorted by name."""
    config = load_config(ctx.paths)
    url = require(config, "moshi")
    if not ctx.keychain.exists(*DEVICE_ENTRY) or bound_origin(ctx.paths, DEVICE) != origin(url):
        raise ApprovalRequired(APPROVAL)
    try:
        body = (api or api_for(url)).installation_status(moshi_token(ctx.paths, ctx.keychain, DEVICE, url))
    except MoshiError as exc:
        raise (ApprovalRequired(APPROVAL) if exc.status == 401 else refusal(exc)) from None
    local = load_agents(ctx.paths, enforce=False)
    here = {a.name.lower() for a in local}
    rows = [_row(ctx, agent, local) for agent in body["agents"]
            if agent.get("kind") == "local" and isinstance(agent.get("name"), str)
            and NAME_RE.fullmatch(agent["name"]) and agent["name"].lower() not in here]
    return sorted(rows, key=lambda row: row["name"].lower())


def _row(ctx: AgentContext, agent: Dict[str, Any], local: List[Agent]) -> Dict[str, Any]:
    def text(key: str, limit: int) -> str:
        value = agent.get(key)
        return clean(value if isinstance(value, str) else "", "", limit=limit)

    harness, skill, active = text("harness", 40), text("skill", 64), text("active_installation", 64)
    known = HARNESSES.get(harness)
    reason = _not_loadable(harness, local)
    return {"name": agent["name"], "harness": harness, "role": text("role", MAX_ROLE),
            "skill": skill if NAME_RE.fullmatch(skill) else "", "active_installation": active or None,
            "harness_installed": bool(known and known.installed(ctx.paths)), "loadable": not reason, "reason": reason}


def _not_loadable(harness: str, local: List[Agent]) -> str:
    """Why `load` would refuse an agent of this harness here; empty when it would not (check_harness)."""
    known = HARNESSES.get(harness)
    if known is None:
        return f"harness {harness} is unknown here" if harness else "no harness in moshi"
    if known.no_own_agents:
        return f"{harness} cannot run agents of its own"
    taken = [a.name for a in local if a.harness == harness]
    if known.single_instance and taken:
        return f"one {harness} agent per Mac, taken by '{taken[0]}'"
    return ""


def set_profile(ctx: AgentContext, name: str, role: Optional[str], skill: Optional[str],
                api: Optional[MoshiApi] = None) -> Tuple[Agent, List[str]]:
    """New role and skill (None leaves one as it is) here and in moshi. moshi first: what it refuses does
    not change here either. Unreachable: changed here only, with one warning."""
    agent = registry.updated(ctx.paths, name, role, skill)
    warnings = _update_remote(ctx, agent, role is not None, skill is not None, api)
    return registry.replace_entry(ctx.paths, ctx.backup, agent), warnings


def _update_remote(ctx: AgentContext, agent: Agent, role_changed: bool, skill_changed: bool,
                   api: Optional[MoshiApi]) -> List[str]:
    config = load_config(ctx.paths)
    if (not (role_changed or skill_changed) or "moshi" not in _servers(ctx, config, agent.harness)
            or not ctx.keychain.exists(keychain_service("moshi", agent.name), agent.name)):
        return []
    url = config.servers["moshi"]
    token = moshi_token(ctx.paths, ctx.keychain, agent.name, url)  # only for the origin it is bound to
    try:
        (api or api_for(url)).update_profile(token, agent.role if role_changed else None,
                                             agent.skill if skill_changed else None)
    except MoshiError as exc:
        if exc.reason == "unreachable":
            return [f"moshi is not reachable: {agent.name} changed on this Mac only."]
        if exc.status == 401:
            raise ValueError(DEAD_TOKEN.format(name=agent.name)) from None
        raise refusal(exc, agent.name) from None
    return []


def remove(ctx: AgentContext, name: str, api: Optional[MoshiApi] = None) -> Tuple[List[str], List[str]]:
    """See removal.remove_agent: (notes, warnings)."""
    from cockpit.removal import remove_agent
    return remove_agent(ctx, name, api)
