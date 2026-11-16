"""`moshi-cockpit install`: idempotent; every file change goes through BackupStore, so `uninstall` can restore it."""

from __future__ import annotations

import json
import subprocess
import time
from dataclasses import dataclass
from typing import Callable, Dict, List, Optional

from cockpit import agents, defaults, herdr
from cockpit.agents import AgentContext
from cockpit.config import SERVERS, _read, load_config, origin, save_servers, validate_url
from cockpit.tokens import ADMIN, bind_identity
from cockpit.defaults import load_json
from cockpit.paths import COCKPIT_IDENTITY, Paths, keychain_service
from cockpit.registry import Agent, load_agents, save_agents
from cockpit.pins import Pin, PinError, verify_codesign

Runner = Callable[..., "subprocess.CompletedProcess[str]"]


@dataclass(frozen=True)
class InstallOptions:
    skip_herdr: bool = False
    moshi: Optional[str] = None    # URL to set, "" to remove, None to leave as it is
    ocellio: Optional[str] = None


def install_herdr(ctx: AgentContext, pins: Dict[str, Pin], runner: Runner = subprocess.run) -> List[str]:
    notes = []
    steps = (
        ("herdr-core", herdr.installed_core_version(ctx.paths), herdr.install_core),
        ("herdr-app", herdr.installed_app_version(ctx.paths), herdr.install_app),
    )
    for component, installed, install in steps:
        pin = pins[component]
        state = herdr.drift(installed, pin)
        if state == "missing":
            notes.append(install(pin, ctx.paths, runner))
            continue
        # Already there: its signature is checked before anything relies on it (nothing is run).
        bundle = herdr.installed_bundle(component, ctx.paths)
        if bundle is None:
            raise PinError(f"{component}: Version gefunden, aber kein App-Bundle (Signatur nicht prüfbar)")
        verify_codesign(bundle, pin, runner)
        if state == "equal":
            notes.append(f"{component} {pin.version} vorhanden (= Pin, Codesignatur geprüft)")
        else:
            shown = ".".join(map(str, installed or ()))
            notes.append(f"{component} {shown} vorhanden (Codesignatur geprüft), Pin {pin.version} ({state}); "
                         "kein Downgrade, siehe doctor")
    return notes


# The agents of a first-generation installation (before agents were named freely). Only the migration
# knows them: a new installation starts with no agents and no servers.
LEGACY_AGENTS = ("vibe", "agy", "codex")


def migrate(ctx: AgentContext) -> List[str]:
    """Carries a first-generation installation over, once ever (marker `migrated` in config.json): its
    agents keep their names and keychain entries, the servers they used become the configured servers.
    Tokens are bound only to origins read from the old agent configs, and only where the configured
    server agrees; anything else stays unbound (no token goes out until an explicit `bot add`)."""
    data = _read(ctx.paths)
    if data.get("migrated"):
        return []
    if ctx.paths.agents_file.exists():
        # An agent list exists already (created by hand or an older run): nothing to carry over, ever again.
        _stamp_migrated(ctx)
        return []
    has = {server: any(ctx.keychain.exists(keychain_service(server, n), n) for n in (*LEGACY_AGENTS, COCKPIT_IDENTITY))
           for server in SERVERS}
    found = [n for n in LEGACY_AGENTS if any(ctx.keychain.exists(keychain_service(s, n), n) for s in SERVERS)]
    if not found and not any(has.values()):
        return []
    notes = []
    config = load_config(ctx.paths)
    known = legacy_servers(ctx.paths)
    missing = {s: known[s] for s in SERVERS if has[s] and not config.has(s) and s in known}
    if missing:
        save_servers(ctx.paths, ctx.backup, missing)
        notes.append("Migration: Server übernommen (" + ", ".join(f"{s} {u}" for s, u in missing.items()) + ")")
    config = load_config(ctx.paths)
    agreed = {s: origin(known[s]) for s in SERVERS
              if s in known and config.has(s) and origin(config.servers[s]) == origin(known[s])}
    migrated = [Agent(name=n, harness=n, home=str(ctx.paths.legacy_vibe_home) if n == "vibe" else None,
                      bound={s: o for s, o in agreed.items() if ctx.keychain.exists(keychain_service(s, n), n)})
                for n in found]
    save_agents(ctx.paths, ctx.backup, migrated)
    if "moshi" in agreed:
        from cockpit.admin import ADMIN_ACCOUNT, ADMIN_SERVICE
        if ctx.keychain.exists(keychain_service("moshi", COCKPIT_IDENTITY), COCKPIT_IDENTITY):
            bind_identity(ctx.paths, ctx.backup, COCKPIT_IDENTITY, config.servers["moshi"])
        if ctx.keychain.exists(ADMIN_SERVICE, ADMIN_ACCOUNT):
            bind_identity(ctx.paths, ctx.backup, ADMIN, config.servers["moshi"])
    _stamp_migrated(ctx)
    notes.append("Migration: Agents übernommen (" + (", ".join(found) or "keine") + "), Namen und Keychain unverändert")
    unbound = sorted(set(SERVERS) & set(known) - set(agreed))
    if unbound:
        notes.append("Migration: " + ", ".join(unbound) + " zeigt in der Config woandershin als in den alten Agent-"
                     "Configs; Tokens bleiben ungebunden, bitte moshi-cockpit bot add <agent> prüfen")
    return notes


def _stamp_migrated(ctx: AgentContext) -> None:
    data = _read(ctx.paths)
    data["migrated"] = True
    ctx.backup.write_text(ctx.paths.mesh_config, json.dumps(data, indent=2) + "\n", mode=0o600)


def legacy_servers(paths) -> Dict[str, str]:
    """The server URLs a first-generation installation used, read from the agent configs it wrote
    (agy's mcp_config.json, the Vibe profile) - so no URL has to be built into moshi-cockpit."""
    found: Dict[str, str] = {}
    servers = load_json(paths.agy_mcp_config).get("mcpServers") or {}
    for name in SERVERS:
        url = (servers.get(name) or {}).get("serverUrl") if isinstance(servers.get(name), dict) else None
        if url:
            found[name] = url
    vibe = paths.legacy_vibe_home / "config.toml"
    if vibe.is_file():
        current = None
        for line in vibe.read_text().splitlines():
            line = line.strip()
            if line.startswith("name = "):
                current = line.split("=", 1)[1].strip().strip('"')
            elif line.startswith("url = ") and current in SERVERS and current not in found:
                found[current] = line.split("=", 1)[1].strip().strip('"')
    valid = {}
    for name, url in found.items():
        try:
            valid[name] = validate_url(url)
        except ValueError:
            continue
    return valid


def setup_agents(ctx: AgentContext) -> List[str]:
    notes = []
    for agent in load_agents(ctx.paths, enforce=False):
        try:
            changed = agents.setup(ctx, agent)
        except ValueError as exc:
            notes.append(str(exc))
            continue
        notes.append(f"{agent.name} ({agent.harness}): eingerichtet"
                     + (f" ({len(changed)} Dateien geändert)" if changed else ", unverändert"))
    return notes


START_TIMEOUT = 90
POLL_SECONDS = 1
HERDR_NOT_READY = ("Herdr ist nicht rechtzeitig bereit geworden: Öffne Herdr.app von Hand und führe danach "
                   "moshi-cockpit install noch einmal aus")


def _herdr(paths: Paths) -> str:
    return str(paths.herdr_app_bundle / "Contents" / "MacOS" / "Herdr")


def _ask(runner: Runner, args: List[str], timeout: int) -> "Optional[subprocess.CompletedProcess[str]]":
    try:
        return runner(args, capture_output=True, text=True, check=False, timeout=timeout)
    except (OSError, subprocess.TimeoutExpired):
        return None


def herdr_answers(paths: Paths, runner: Runner) -> bool:
    """herdr's own layout exists and the running window names the layout it shows."""
    if not defaults.main_layout_ready(paths):
        return False
    done = _ask(runner, [_herdr(paths), "status"], 5)
    if done is None or done.returncode != 0:
        return False
    try:
        layout = json.loads(done.stdout).get("layout")
    except (ValueError, AttributeError):
        return False
    return isinstance(layout, str) and bool(layout)


def start_herdr(paths: Paths, runner: Runner, sleep: Callable[[float], None] = time.sleep,
                now: Callable[[], float] = time.monotonic) -> bool:
    """Opens Herdr.app and waits until it is ready. Only called after install_herdr checked the signature."""
    opened = _ask(runner, ["open", str(paths.herdr_app_bundle)], 30)
    if opened is None or opened.returncode != 0:
        return False
    deadline = now() + START_TIMEOUT
    while not herdr_answers(paths, runner):
        if now() >= deadline:
            return False
        sleep(POLL_SECONDS)
    return True


def show_cockpit_layout(paths: Paths, runner: Runner) -> str:
    done = _ask(runner, [_herdr(paths), "do", "layout.switch", json.dumps({"name": defaults.COCKPIT_LAYOUT})], 10)
    if done is not None and done.returncode == 0:
        return "Herdr zeigt das Layout cockpit"
    return "Layout cockpit ist angelegt: In Herdr mit ⌘⇧L einschalten"


def run(ctx: AgentContext, pins: Dict[str, Pin], options: InstallOptions,
        runner: Runner = subprocess.run, say: Callable[[str], None] = print,
        sleep: Callable[[float], None] = time.sleep, now: Callable[[], float] = time.monotonic) -> List[str]:
    notes: List[str] = []
    if not options.skip_herdr:
        notes.extend(install_herdr(ctx, pins, runner))
    if defaults.ensure_telemetry_off(ctx.paths, ctx.backup):
        notes.append("herdr-app: Telemetrie ausgeschaltet")
    else:
        notes.append("herdr-app: Telemetrie war schon aus")
    first_start = not options.skip_herdr and not defaults.main_layout_ready(ctx.paths)
    ready = True
    if first_start:
        say("• Herdr startet zum ersten Mal, das dauert einen Moment")
        ready = start_herdr(ctx.paths, runner, sleep, now)
    mods = defaults.install_mods(ctx.paths, ctx.backup)
    notes.append("herdr-Mods: " + (", ".join(mods) if mods else "unverändert"))
    old_key = defaults.remove_panel_key(ctx.paths, ctx.backup)
    if old_key:
        notes.append(old_key)
    notes.append(defaults.ensure_claude_launcher(ctx.paths, ctx.backup))
    defaults.install_icons(ctx.paths, ctx.backup)
    notes.append(defaults.ensure_vibe_entry(ctx.paths, ctx.backup))
    notes.append(defaults.sync_agent_menu(ctx.paths, ctx.backup))
    if ready:  # otherwise the last note says what to do, once
        notes.append(defaults.install_cockpit_layout(ctx.paths, ctx.backup))
    linked = agents.link_launchers(ctx)
    notes.append("Launcher verlinkt: " + (", ".join(linked) if linked else "unverändert"))
    servers = {name: url for name, url in (("moshi", options.moshi), ("ocellio", options.ocellio)) if url is not None}
    if servers:
        changed = save_servers(ctx.paths, ctx.backup, servers)
        notes.append("Server: " + ", ".join(f"{n} {u or '(entfernt)'}" for n, u in servers.items())
                     + ("" if changed else ", unverändert"))
    notes.extend(migrate(ctx))
    notes.extend(setup_agents(ctx))
    if first_start:
        notes.append(show_cockpit_layout(ctx.paths, runner) if ready else HERDR_NOT_READY)
    return notes
