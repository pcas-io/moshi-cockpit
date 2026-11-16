"""`moshi-cockpit doctor`: one check per concern, FAIL makes the exit code non-zero, WARN informs."""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from typing import Callable, List, Optional

from cockpit import agents, defaults, herdr
from cockpit.admin import ADMIN_ACCOUNT, ADMIN_SERVICE
from cockpit.keychain import Keychain, pending_service, stale_parked_message
from cockpit.mcp import McpError, probe_unauthenticated
from cockpit.config import load_config, origin
from cockpit.paths import COCKPIT_IDENTITY, Paths, keychain_service
from cockpit.registry import load_agents
from cockpit.pins import Pin, PinError, verify_codesign
from cockpit.text import clean

OK, WARN, FAIL = "ok", "warn", "fail"


@dataclass(frozen=True)
class Check:
    name: str
    status: str
    detail: str


@dataclass(frozen=True)
class DoctorContext:
    paths: Paths
    keychain: Keychain
    pins: dict
    probe: Callable = probe_unauthenticated
    core_version: Callable = herdr.installed_core_version
    codesign: Callable = verify_codesign
    bundle: Callable = herdr.installed_bundle


def check_versions(ctx: DoctorContext) -> List[Check]:
    results = []
    sources = (("herdr-core", ctx.core_version(ctx.paths)), ("herdr-app", herdr.installed_app_version(ctx.paths)))
    for component, installed in sources:
        pin: Pin = ctx.pins[component]
        state = herdr.drift(installed, pin)
        shown = ".".join(map(str, installed)) if installed else "–"
        if state == "missing":
            results.append(Check(component, FAIL, f"nicht installiert (Pin {pin.version}), moshi-cockpit install"))
            continue
        bundle = ctx.bundle(component, ctx.paths)
        if bundle is None:
            results.append(Check(component, FAIL, f"{shown} gemeldet, aber kein App-Bundle gefunden (Signatur nicht prüfbar)"))
            continue
        try:
            ctx.codesign(bundle, pin)
        except PinError as exc:
            results.append(Check(component, FAIL, f"{shown} installiert, aber {exc}"))
            continue
        if state == "equal":
            results.append(Check(component, OK, f"{shown} = Pin"))
        else:
            # Herdr.app updates itself and cannot be stopped from doing so: report, don't fail.
            results.append(Check(component, WARN, f"{shown} ist {'neuer' if state == 'newer' else 'älter'} "
                                                  f"als der Pin {pin.version}; Pin prüfen und per PR anheben"))
    return results


def check_telemetry(ctx: DoctorContext) -> List[Check]:
    if defaults.telemetry_off(ctx.paths):
        return [Check("telemetry", OK, "herdr-app Telemetrie aus")]
    return [Check("telemetry", FAIL, f"Telemetrie nicht aus ({ctx.paths.herdr_settings}), moshi-cockpit install")]


def check_permissions(ctx: DoctorContext) -> List[Check]:
    findings = defaults.permission_findings(ctx.paths)
    if not findings:
        return [Check("permissions", OK, "Config, App-Home und Socket nur für dich schreibbar")]
    return [Check("permissions", FAIL, "; ".join(findings))]


def check_mods(ctx: DoctorContext) -> List[Check]:
    unknown = defaults.unknown_mods(ctx.paths)
    if not unknown:
        return [Check("mods", OK, "nur bekannte Mods")]
    return [Check("mods", WARN, "unbekannte Mods (Mods laufen mit vollen Rechten, bitte prüfen): " + ", ".join(unknown))]


def check_cockpit(ctx: DoctorContext) -> List[Check]:
    findings = defaults.cockpit_findings(ctx.paths)
    if not findings:
        return [Check("cockpit", OK, "Layout cockpit eingerichtet")]
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


def parked_entries(ctx: DoctorContext) -> List[tuple]:
    names = [a.name for a in _agents(ctx)[0]]
    entries = [(keychain_service(server, name), name) for name in names for server in ("ocellio", "moshi")]
    return entries + [(keychain_service("moshi", COCKPIT_IDENTITY), COCKPIT_IDENTITY), (ADMIN_SERVICE, ADMIN_ACCOUNT)]


def check_config(ctx: DoctorContext) -> List[Check]:
    """Servers are optional (the cockpit also works without them); a broken config or an invalid URL is an error."""
    config, error = _config(ctx)
    if error:
        return [Check("config", FAIL, error)]
    if not config.servers:
        return [Check("config", OK, "keine Server eingerichtet: Agents lokal, ohne moshi und ocellio")]
    detail = ", ".join(f"{name} {url}" for name, url in sorted(config.servers.items()))
    who = f"; Person {config.person}" if config.person else ""
    return [Check("config", OK, detail + who)]


def check_identities(ctx: DoctorContext) -> List[Check]:
    """The cockpit identity, the admin token and the registration token are origin-bound like agents
    (tokens.py). Only whether an entry exists is asked; no token is read."""
    config, error = _config(ctx)
    if error or not config.has("moshi"):
        return []
    from cockpit.admin import ADMIN_ACCOUNT, ADMIN_SERVICE
    from cockpit.register import REGISTER_ACCOUNT, REGISTER_SERVICE
    from cockpit.tokens import ADMIN, REGISTER, check
    results = []
    for identity, service, account in ((COCKPIT_IDENTITY, keychain_service("moshi", COCKPIT_IDENTITY), COCKPIT_IDENTITY),
                                       (ADMIN, ADMIN_SERVICE, ADMIN_ACCOUNT),
                                       (REGISTER, REGISTER_SERVICE, REGISTER_ACCOUNT)):
        if not ctx.keychain.exists(service, account):
            continue
        try:
            check(ctx.paths, identity, config.servers["moshi"])
            results.append(Check(f"identity-{identity}", OK, f"an {origin(config.servers['moshi'])} gebunden"))
        except ValueError as exc:
            results.append(Check(f"identity-{identity}", FAIL, str(exc)))
    return results


def check_rules(ctx: DoctorContext) -> List[Check]:
    """Own rules reach an agent at its start (AGENTS.md, start instructions, system prompt); grok not yet."""
    config, error = _config(ctx)
    registered, _ = _agents(ctx)
    if error or not config.rules:
        return []
    from cockpit.harnesses import HARNESSES
    outside = sorted(a.name for a in registered if not HARNESSES[a.harness].native_mesh)
    if not outside:
        return [Check("rules", OK, "eigene Regeln gehen an alle Agents")]
    return [Check("rules", WARN, "eigene Regeln erreichen " + ", ".join(outside) + " nicht automatisch: dort selbst "
                  "pflegen (Konto-Einstellungen)")]


def check_menu(ctx: DoctorContext) -> List[Check]:
    from cockpit.defaults import menu_findings
    return [Check("menu", WARN, finding) for finding in menu_findings(ctx.paths)]


def check_parked(ctx: DoctorContext) -> List[Check]:
    """Parked copies of an interrupted strict keychain rewrite (keychain.py): never silently left over."""
    findings = []
    for service, account in parked_entries(ctx):
        if not ctx.keychain.has_parked(service, account):
            continue
        if ctx.keychain.exists(service, account):
            findings.append(stale_parked_message(service, account))
        else:
            findings.append(f"'{service}' fehlt, die Sicherung '{pending_service(service)}' ist da: "
                            f"moshi-cockpit bot add … --strict-keychain stellt sie wieder her")
    if not findings:
        return [Check("keychain-parked", OK, "keine liegengebliebenen Sicherungen")]
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
            results.append(Check(f"endpoint-{name}", OK, f"{url} antwortet 401 + Bearer"))
        else:
            results.append(Check(f"endpoint-{name}", FAIL, f"{url} antwortet {status} (erwartet 401 + Bearer)"))
    return results


def check_agents(ctx: DoctorContext) -> List[Check]:
    config, error = _config(ctx)
    registered, agents_error = _agents(ctx)
    if error or agents_error:
        return [Check("agents", FAIL, agents_error or "Config fehlerhaft, Agents nicht prüfbar")]
    if not registered:
        return [Check("agents", OK, "noch keine Agents (moshi-cockpit agent add)")]
    actx = agents.AgentContext(paths=ctx.paths, backup=None, keychain=ctx.keychain)
    from cockpit.registry import single_instance_problems
    results = [Check("agents", FAIL, problem) for problem in single_instance_problems(registered)]
    for agent in registered:
        findings = agents.findings(actx, agent)
        label = f"{agent.harness}" + (f", {agent.role}" if agent.role else "")
        results.append(Check(f"agent-{agent.name}", FAIL if findings else OK,
                             "; ".join(findings) or f"{label}: Keychain + Config ok"))
    return results


CHECKS = (check_versions, check_telemetry, check_permissions, check_mods, check_cockpit, check_config,
          check_endpoints, check_agents, check_identities, check_rules, check_menu, check_parked)


def run(ctx: DoctorContext) -> List[Check]:
    results: List[Check] = []
    for check in CHECKS:
        results.extend(check(ctx))
    return results


INSTALL = "Führe moshi-cockpit install aus"


def _action(check: Check) -> str:
    """The problem and the one thing to do about it, as a single sentence."""
    name, detail = check.name, check.detail
    if name in ("herdr-core", "herdr-app"):
        if "nicht installiert" in detail:
            return f"{name} fehlt: {INSTALL}"
        return f"{name} ist nicht vertrauenswürdig installiert: Entferne es und führe moshi-cockpit install aus"
    if name == "telemetry":
        return f"Die Telemetrie von Herdr ist nicht aus: {INSTALL}"
    if name == "permissions":
        if "schreibbar" in detail:
            return f"Nimm anderen die Schreibrechte mit chmod go-w: {detail}"
        return f"Prüfe diesen Pfad: {detail}"
    if name == "config":
        return f"Korrigiere deine Konfiguration: {detail}"
    if name.startswith("endpoint-"):
        server = name[len("endpoint-"):]
        if "nicht erreichbar" in detail:
            return f"{server} ist nicht erreichbar: Prüfe deine Internetverbindung"
        return f"{server} antwortet unerwartet: Prüfe die Server-Adresse ({detail})"
    if name.startswith("agent-"):
        return _agent_action(name[len("agent-"):], detail)
    return detail  # agents, identity-…: these details already name their way out


def _agent_action(agent: str, detail: str) -> str:
    """Goes by the first kind of finding, in the order agents.findings reports them."""
    if "Keychain-Eintrag fehlt" in detail or "an keinen Server gebunden" in detail:
        return f"Agent {agent} ist nicht vollständig angebunden: Führe moshi-cockpit bot add {agent} aus"
    if "Origin-Mismatch" in detail:
        return f"Agent {agent} ist an einen anderen Server gebunden: Führe moshi-cockpit bot add {agent} --rebind aus, wenn der Umzug Absicht ist"
    if "nicht installiert" in detail:
        return f"Agent {agent}: {detail}. Installiere die CLI"
    if "nicht 0600" in detail:
        return f"Agent {agent}: {detail}. Setze die Datei mit chmod 600 zurück"
    if "fehlt (moshi-cockpit install)" in detail or "weicht von dem ab" in detail:
        return f"Die Einrichtung von Agent {agent} weicht ab: {INSTALL}"
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
    lines.append(f"\n{len(results)} Prüfungen · {fails} Fehler · {warns} Hinweise")
    return "\n".join(lines)
