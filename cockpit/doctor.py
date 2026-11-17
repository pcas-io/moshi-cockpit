"""`moshi-cockpit doctor`: one check per concern, FAIL makes the exit code non-zero, WARN informs."""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from typing import Callable, List, Optional, Tuple

from cockpit import agents, compat, defaults
from cockpit.compat import FAIL, OK, WARN
from cockpit.secrets import HardenedStore, SecretStore
from cockpit.secrets.macos import pending_service, stale_parked_message
from cockpit.mcp import McpError, probe_unauthenticated
from cockpit.config import load_config, origin
from cockpit.paths import Paths, keychain_service
from cockpit.registry import load_agents
from cockpit.text import clean
from cockpit.tokens import DEVICE, NON_AGENT, OCELLIO_DEFAULT


@dataclass(frozen=True)
class Check:
    name: str
    status: str
    detail: str


@dataclass(frozen=True)
class DoctorContext:
    paths: Paths
    keychain: SecretStore
    probe: Callable = probe_unauthenticated
    herdr_versions: Callable[[Paths], Tuple[Optional[str], Optional[str]]] = compat.installed_versions


def check_herdr(ctx: DoctorContext) -> List[Check]:
    """Herdr is installed from herdr.dev; a version this release was not tested with only warns."""
    return [Check(f.name, f.status, f.detail) for f in compat.check(*ctx.herdr_versions(ctx.paths))]


def check_telemetry(ctx: DoctorContext) -> List[Check]:
    if defaults.telemetry_off(ctx.paths):
        return [Check("telemetry", OK, "Herdr telemetry is off")]
    return [Check("telemetry", FAIL, f"Telemetry is not off ({ctx.paths.herdr_settings}), run moshi-cockpit install")]


def check_permissions(ctx: DoctorContext) -> List[Check]:
    findings = defaults.permission_findings(ctx.paths)
    if not findings:
        return [Check("permissions", OK, "Config, app home and socket are writable by you only")]
    return [Check("permissions", FAIL, "; ".join(findings))]


def check_mods(ctx: DoctorContext) -> List[Check]:
    unknown = defaults.unknown_mods(ctx.paths)
    if not unknown:
        return [Check("mods", OK, "known mods only")]
    return [Check("mods", WARN, "unknown mods (mods run with full rights, please check them): " + ", ".join(unknown))]


def check_cockpit(ctx: DoctorContext) -> List[Check]:
    findings = defaults.cockpit_findings(ctx.paths)
    if not findings:
        return [Check("cockpit", OK, "cockpit layout set up")]
    return [Check("cockpit", WARN, "; ".join(findings) + ". " + defaults.RERUN_HINT)]


def _config(ctx: DoctorContext):
    try:
        return load_config(ctx.paths), None
    except ValueError as exc:
        return None, str(exc)


def _agents(ctx: DoctorContext):
    try:
        return load_agents(ctx.paths, enforce=False), None
    except ValueError as exc:
        return [], str(exc)


def _non_agent_entry(identity: str) -> Tuple[str, str]:
    """Keychain service and account of a token that is not an agent's (see tokens.py)."""
    if identity == OCELLIO_DEFAULT:
        return keychain_service("ocellio", "default"), "default"
    return keychain_service("moshi", identity), identity


def parked_entries(ctx: DoctorContext) -> List[tuple]:
    names = [a.name for a in _agents(ctx)[0]]
    entries = [(keychain_service(server, name), name) for name in names for server in ("ocellio", "moshi")]
    return entries + [_non_agent_entry(identity) for identity in NON_AGENT]


def check_config(ctx: DoctorContext) -> List[Check]:
    """Servers are optional (the cockpit also works without them); a broken config or an invalid URL is an error."""
    config, error = _config(ctx)
    if error:
        return [Check("config", FAIL, error)]
    if not config.servers:
        return [Check("config", OK, "no servers set up: agents run locally, without moshi and ocellio")]
    detail = ", ".join(f"{name} {url}" for name, url in sorted(config.servers.items()))
    return [Check("config", OK, detail)]


def check_identities(ctx: DoctorContext) -> List[Check]:
    """The device key is origin-bound like an agent's token (tokens.py). Only whether the entry exists is
    asked; no token is read."""
    config, error = _config(ctx)
    if error or not config.has("moshi"):
        return []
    from cockpit.tokens import check
    service, account = _non_agent_entry(DEVICE)
    if not ctx.keychain.exists(service, account):
        return []
    try:
        check(ctx.paths, DEVICE, config.servers["moshi"])
    except ValueError as exc:
        return [Check(f"identity-{DEVICE}", FAIL, str(exc))]
    return [Check(f"identity-{DEVICE}", OK, f"bound to {origin(config.servers['moshi'])}")]


def check_rules(ctx: DoctorContext) -> List[Check]:
    """Own rules reach an agent at its start (AGENTS.md, start instructions, system prompt); grok not yet."""
    config, error = _config(ctx)
    registered, _ = _agents(ctx)
    if error or not config.rules:
        return []
    from cockpit.harnesses import HARNESSES
    outside = sorted(a.name for a in registered if not HARNESSES[a.harness].native_mesh)
    if not outside:
        return [Check("rules", OK, "your own rules reach every agent")]
    return [Check("rules", WARN, "your own rules do not reach " + ", ".join(outside) + " by themselves: keep them "
                  "there yourself (account settings)")]


def check_menu(ctx: DoctorContext) -> List[Check]:
    from cockpit.defaults import menu_findings
    return [Check("menu", WARN, finding) for finding in menu_findings(ctx.paths)]


def check_parked(ctx: DoctorContext) -> List[Check]:
    """Parked copies of an interrupted hardened rewrite (secrets/macos.py): never silently left over. Only a
    store that hardens entries parks copies; any other store has nothing to report."""
    store = ctx.keychain
    if not isinstance(store, HardenedStore):
        return []
    findings = []
    for service, account in parked_entries(ctx):
        if not store.has_parked(service, account):
            continue
        if store.exists(service, account):
            findings.append(stale_parked_message(service, account))
        else:
            findings.append(f"'{service}' is missing, its hardened backup '{pending_service(service)}' is still "
                            "there: store the token again, that replaces the backup")
    if not findings:
        return [Check("keychain-parked", OK, "no backups left over")]
    return [Check("keychain-parked", WARN, " | ".join(findings))]


def check_endpoints(ctx: DoctorContext) -> List[Check]:
    """Only configured servers are probed; an unset one is not an error."""
    config, _ = _config(ctx)
    results = []
    for name, url in sorted((config.servers if config else {}).items()):
        try:
            status, challenge = ctx.probe(url)
        except McpError as exc:
            results.append(Check(f"endpoint-{name}", FAIL, str(exc)))
            continue
        if status == 401 and challenge.lower().startswith("bearer"):
            results.append(Check(f"endpoint-{name}", OK, f"{url} answers 401 + Bearer"))
        else:
            results.append(Check(f"endpoint-{name}", FAIL, f"{url} answers {status} (expected 401 + Bearer)"))
    return results


def check_agents(ctx: DoctorContext) -> List[Check]:
    config, error = _config(ctx)
    registered, agents_error = _agents(ctx)
    if error or agents_error:
        return [Check("agents", FAIL, agents_error or "config is broken, agents cannot be checked")]
    if not registered:
        return [Check("agents", OK, "no agents yet (moshi-cockpit agent add)")]
    actx = agents.AgentContext(paths=ctx.paths, backup=None, keychain=ctx.keychain)
    from cockpit.registry import single_instance_problems
    results = [Check("agents", FAIL, problem) for problem in single_instance_problems(registered)]
    for agent in registered:
        findings = agents.findings(actx, agent)
        label = f"{agent.harness}" + (f", {agent.role}" if agent.role else "")
        results.append(Check(f"agent-{agent.name}", FAIL if findings else OK,
                             "; ".join(findings) or f"{label}: Keychain and config ok"))
    return results


CHECKS = (check_herdr, check_telemetry, check_permissions, check_mods, check_cockpit, check_config,
          check_endpoints, check_agents, check_identities, check_rules, check_menu, check_parked)


def run(ctx: DoctorContext) -> List[Check]:
    results: List[Check] = []
    for check in CHECKS:
        results.extend(check(ctx))
    return results


INSTALL = "Run moshi-cockpit install"


def _action(check: Check) -> str:
    """The problem and the one thing to do about it, as a single sentence."""
    name, detail = check.name, check.detail
    if name == "herdr":
        return f"Herdr is not installed: Install it from {compat.HERDR_URL}, then run moshi-cockpit install"
    if name == "telemetry":
        return f"Herdr's telemetry is not off: {INSTALL}"
    if name == "permissions":
        if "schreibbar" in detail:  # defaults.permission_findings
            return f"Take the write permission away from others with chmod go-w: {detail}"
        return f"Check this path: {detail}"
    if name == "config":
        return f"Fix your configuration: {detail}"
    if name.startswith("endpoint-"):
        server = name[len("endpoint-"):]
        if "nicht erreichbar" in detail:  # mcp.probe_unauthenticated
            return f"{server} is not reachable: Check your internet connection"
        return f"{server} answers unexpectedly: Check the server address ({detail})"
    if name.startswith("agent-"):
        return _agent_action(name[len("agent-"):], detail)
    return detail  # agents, identity-…: these details already name their way out


def _agent_action(agent: str, detail: str) -> str:
    """Goes by the first kind of finding, in the order agents.findings reports them (its wording)."""
    if "Keychain entry missing" in detail or "not bound to a server" in detail:
        return f"Agent {agent} is not fully connected to the servers: {detail}"
    if "Origin mismatch" in detail:
        return f"Agent {agent} is bound to another server than the configured one: {detail}"
    if "not installed" in detail:
        return f"Agent {agent}: {detail}. Install the CLI"
    if "nicht 0600" in detail:
        return f"Agent {agent}: {detail}. Reset the file with chmod 600"
    if "fehlt (moshi-cockpit install)" in detail or "weicht von dem ab" in detail:
        return f"The setup of agent {agent} differs: {INSTALL}"
    return f"Agent {agent}: {detail}"


def quiet(results: List[Check]) -> Optional[str]:
    """Nothing while all is well. Otherwise the first error with its one action: the next one after that is fixed."""
    for result in results:
        if result.status == FAIL:
            return clean(_action(result), "?", limit=400)
    return None


def exit_code(results: List[Check]) -> int:
    return 1 if any(r.status == FAIL for r in results) else 0


def render(results: List[Check], as_json: bool = False) -> str:
    if as_json:
        return json.dumps([asdict(r) for r in results], indent=2, ensure_ascii=False)
    marks = {OK: "✓", WARN: "!", FAIL: "✗"}
    lines = [f"{marks[r.status]} {r.name:<18} {r.detail}" for r in results]
    fails = sum(r.status == FAIL for r in results)
    warns = sum(r.status == WARN for r in results)
    lines.append(f"\n{len(results)} checks · {fails} errors · {warns} warnings")
    return "\n".join(lines)
