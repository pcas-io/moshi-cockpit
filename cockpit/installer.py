"""`moshi-cockpit install`: idempotent; every file change goes through BackupStore, so `uninstall` can restore it.

Herdr itself comes from its vendor: install refuses to run without /Applications/Herdr.app and never downloads,
replaces or verifies it (doctor reports versions this release was not tested with). A headless Linux machine has
no Herdr app: Herdr on the Mac drives it, so install sets up only the servers, the launcher and the agents there.
"""

from __future__ import annotations

import json
import subprocess
import time
from dataclasses import dataclass
from typing import Callable, Dict, List, Optional

from cockpit import agents, claude_hook, compat, defaults, legacy, mesh_files
from cockpit.agents import AgentContext
from cockpit.config import SERVERS, _read, load_config, origin, save_servers, validate_url, with_default_path
from cockpit.defaults import load_json
from cockpit.paths import Paths, keychain_service
from cockpit.registry import Agent, load_agents, save_agents

Runner = Callable[..., "subprocess.CompletedProcess[str]"]
HERDR_MISSING = f"Install Herdr from {compat.HERDR_URL} first, then run this again."
HEADLESS_NOTE = ("Headless install: no Herdr app on this machine. Start its agents from Herdr on your Mac "
                 "(+ Agent → New, Where: this machine)")


@dataclass(frozen=True)
class InstallOptions:
    moshi: Optional[str] = None    # URL to set, "" to remove, None to leave as it is
    ocellio: Optional[str] = None


# The agents of a first-generation installation (before agents were named freely). Only the migration
# knows them: a new installation starts with no agents and no servers.
LEGACY_AGENTS = ("vibe", "agy", "codex")


def migrate(ctx: AgentContext) -> List[str]:
    """Carries a first-generation installation over, once ever (marker `migrated` in config.json): its
    agents keep their names and keychain entries, the servers they used become the configured servers.
    Tokens are bound only to origins read from the old agent configs, and only where the configured
    server agrees; anything else stays unbound (no token goes out until it is bound on purpose)."""
    data = _read(ctx.paths)
    if data.get("migrated"):
        return []
    if ctx.paths.agents_file.exists():
        # An agent list exists already (created by hand or an older run): nothing to carry over, ever again.
        _stamp_migrated(ctx)
        return []
    has = {server: any(ctx.keychain.exists(keychain_service(server, n), n) for n in LEGACY_AGENTS)
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
        notes.append("Migration: servers carried over (" + ", ".join(f"{s} {u}" for s, u in missing.items()) + ")")
    config = load_config(ctx.paths)
    agreed = {s: origin(known[s]) for s in SERVERS
              if s in known and config.has(s) and origin(config.servers[s]) == origin(known[s])}
    migrated = [Agent(name=n, harness=n, home=str(ctx.paths.legacy_vibe_home) if n == "vibe" else None,
                      bound={s: o for s, o in agreed.items() if ctx.keychain.exists(keychain_service(s, n), n)})
                for n in found]
    save_agents(ctx.paths, ctx.backup, migrated)
    _stamp_migrated(ctx)
    notes.append("Migration: agents carried over (" + (", ".join(found) or "none") + "), names and Keychain unchanged")
    unbound = sorted(set(SERVERS) & set(known) - set(agreed))
    if unbound:
        notes.append("Migration: " + ", ".join(unbound) + " points somewhere else in the config than in the old agent "
                     "configs; their tokens stay unbound and are not sent")
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
        notes.append(f"{agent.name} ({agent.harness}): set up"
                     + (f" ({len(changed)} files changed)" if changed else ", unchanged"))
    return notes


START_TIMEOUT = 90
POLL_SECONDS = 1
HERDR_NOT_READY = ("Herdr did not get ready in time: Open Herdr.app yourself, then run moshi-cockpit install "
                   "again")
LAYOUT_SHOWN = "Herdr shows the cockpit layout"


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
    """Opens Herdr.app and waits until it is ready."""
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
        return LAYOUT_SHOWN
    return "The cockpit layout is set up: switch to it in Herdr with ⌘⇧L"


def run(ctx: AgentContext, options: InstallOptions, runner: Runner = subprocess.run,
        say: Callable[[str], None] = print, sleep: Callable[[float], None] = time.sleep,
        now: Callable[[], float] = time.monotonic) -> List[str]:
    if ctx.paths.headless:
        return _run_headless(ctx, options)
    if not ctx.paths.herdr_app_bundle.is_dir():
        raise ValueError(HERDR_MISSING)  # before anything is written
    moved = legacy.import_config(ctx.paths, ctx.backup, ctx.keychain)  # before this install writes its own
    notes: List[str] = list(moved)
    if defaults.ensure_telemetry_off(ctx.paths, ctx.backup):
        notes.append("Herdr: telemetry switched off")
    else:
        notes.append("Herdr: telemetry was off already")
    first_start = not defaults.main_layout_ready(ctx.paths)
    ready = True
    if first_start:
        say("• Herdr is starting for the first time, this takes a moment")
        ready = start_herdr(ctx.paths, runner, sleep, now)
    mods = defaults.install_mods(ctx.paths, ctx.backup)
    notes.append("Herdr mods: " + (", ".join(mods) if mods else "unchanged"))
    old_key = defaults.remove_panel_key(ctx.paths, ctx.backup)
    if old_key:
        notes.append(old_key)
    notes.append(defaults.ensure_claude_launcher(ctx.paths, ctx.backup))
    notes.append(defaults.ensure_vibe_entry(ctx.paths, ctx.backup))
    notes.append(defaults.sync_agent_menu(ctx.paths, ctx.backup))
    if ready:  # otherwise the last note says what to do, once
        notes.append(defaults.install_cockpit_layout(ctx.paths, ctx.backup))
    notes.append(_link_launchers(ctx))
    notes.extend(_save_servers(ctx, options))
    notes.extend(migrate(ctx))
    notes.extend(setup_agents(ctx))
    notes.extend(_mesh_notes(ctx))
    if first_start:
        notes.append(show_cockpit_layout(ctx.paths, runner) if ready else HERDR_NOT_READY)
    if moved:
        notes.append(legacy.FOUND)
    return notes


def _run_headless(ctx: AgentContext, options: InstallOptions) -> List[str]:
    """No Herdr app here: no legacy import, telemetry, mods, menu, layout or Herdr start."""
    notes = [HEADLESS_NOTE]
    notes.extend(_save_servers(ctx, options))
    notes.append(_link_launchers(ctx))
    notes.extend(setup_agents(ctx))
    notes.extend(_mesh_notes(ctx))
    return notes


def _mesh_notes(ctx: AgentContext) -> List[str]:
    """What agents need to find and reach each other: the agent-mesh skill, a template per agent, the warning in
    Claude sessions started without an identity."""
    return [*(mesh_files.install_skill(ctx.paths, ctx.backup, skill) for skill in mesh_files.SKILLS),
            mesh_files.sync_templates(ctx.paths, ctx.backup), claude_hook.ensure(ctx.paths)]


def _link_launchers(ctx: AgentContext) -> str:
    linked = agents.link_launchers(ctx)
    return "Launchers linked: " + (", ".join(linked) if linked else "unchanged")


def _save_servers(ctx: AgentContext, options: InstallOptions) -> List[str]:
    servers = {name: with_default_path(name, url) if url else url
               for name, url in (("moshi", options.moshi), ("ocellio", options.ocellio)) if url is not None}
    if not servers:
        return []
    changed = save_servers(ctx.paths, ctx.backup, servers)
    return ["Servers: " + ", ".join(f"{n} {u or '(removed)'}" for n, u in servers.items())
            + ("" if changed else ", unchanged")]
