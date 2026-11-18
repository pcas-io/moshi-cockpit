"""Secure defaults for herdr-app and the permission/mod checks `doctor` relies on."""

from __future__ import annotations

import json
import os
import stat
from pathlib import Path
from typing import Dict, List, Mapping, Optional, Set

from cockpit.backup import BackupStore
from cockpit.paths import SHARE, Paths


def ensure_telemetry_off(paths: Paths, backup: BackupStore) -> bool:
    """Merges `"telemetry": false` into herdr-app's settings.json, keeping every other key."""
    settings = load_json(paths.herdr_settings)
    if settings.get("telemetry") is False:
        return False
    settings["telemetry"] = False
    return backup.write_text(paths.herdr_settings, json.dumps(settings, indent=2) + "\n")


def telemetry_off(paths: Paths) -> bool:
    return load_json(paths.herdr_settings).get("telemetry") is False


def writable_by_others(path: Path) -> bool:
    """True when group or others may write. A missing path is not a finding."""
    try:
        mode = path.lstat().st_mode
    except FileNotFoundError:
        return False
    return bool(mode & (stat.S_IWGRP | stat.S_IWOTH))


def permission_findings(paths: Paths, uid: Optional[int] = None) -> List[str]:
    findings = []
    for path in (paths.config, paths.herdr_app_home, paths.herdr_socket):
        if writable_by_others(path):
            findings.append(f"{path} is writable by group or others")
    findings.extend(socket_findings(paths.herdr_socket, os.getuid() if uid is None else uid))
    return findings


def socket_findings(sock: Path, uid: int) -> List[str]:
    """herdr's control socket has no authentication of its own: whoever can connect drives the window.

    Connecting to a Unix socket needs write permission on it, so group/other write bits are the ones
    that matter (herdr creates it 0755, which is safe). Checked as well: it is a socket, not a link or
    file someone put there, it and its folder belong to you, and the folder lets nobody else write.
    A missing socket (Herdr not running) is not a finding.
    """
    try:
        st = sock.lstat()
    except FileNotFoundError:
        return []
    findings = []
    if not stat.S_ISSOCK(st.st_mode):
        findings.append(f"{sock} is not a socket (check what it is; Herdr did not create it?)")
    if st.st_uid != uid:
        findings.append(f"{sock} is not yours (uid {st.st_uid})")
    parent = sock.parent.lstat()
    if parent.st_uid != uid:
        findings.append(f"{sock.parent} is not yours (uid {parent.st_uid})")
    if parent.st_mode & (stat.S_IWGRP | stat.S_IWOTH):
        findings.append(f"{sock.parent} is writable by group or others (the socket could be swapped)")
    return findings


def known_mods(paths: Paths) -> Set[str]:
    """Mods herdr shipped (installed.json) plus our allowlist."""
    shipped = {name for name in load_json(paths.herdr_installed) if not name.startswith("layout:")}
    allowed = set(load_json(SHARE / "mods-allowlist.json").get("allowed", []))
    return shipped | allowed


def unknown_mods(paths: Paths) -> List[str]:
    if not paths.herdr_mods.is_dir():
        return []
    known = known_mods(paths)
    return sorted(p.name for p in paths.herdr_mods.iterdir() if p.is_dir() and p.name not in known)


RERUN_HINT = "Open Herdr once, then run moshi-cockpit install again"

def cockpit_findings(paths: Paths) -> List[str]:
    """What `install` could not set up yet (typically: Herdr was never started before it ran)."""
    findings = []
    if not (paths.herdr_app_home / "layouts" / f"{COCKPIT_LAYOUT}.json").is_file():
        findings.append("The cockpit layout is missing")
    return findings


COCKPIT_LAYOUT = "cockpit"
OWN_AGENTS_BLOCK = {"id": "own-agents", "type": "block", "size": {"weight": 1}, "min": 90, "content": {"mod": "moshi"}}
# Earlier versions docked moshi on the right and bound ⌘⇧M to it.
OLD_PANEL_KEY = "cmd-shift-m"
OLD_PANEL_BINDINGS = {"global": "mod.open:moshi", f"layout:{COCKPIT_LAYOUT}": "layout.toggle:moshi"}


def remove_panel_key(paths: Paths, backup: BackupStore) -> Optional[str]:
    """Takes back the key the old panel used. Only where it still does what we bound; a note if something changed."""
    keys_file = paths.herdr_app_home / "keys.json"
    keys = load_json(keys_file)
    changed = False
    for section, action in OLD_PANEL_BINDINGS.items():
        current = keys.get(section)
        if isinstance(current, dict) and current.get(OLD_PANEL_KEY) == action:
            del current[OLD_PANEL_KEY]
            if not current:
                del keys[section]
            changed = True
    if not changed:
        return None
    backup.write_text(keys_file, json.dumps(keys, indent=2) + "\n")
    return "⌘⇧M: removed the old binding of the moshi panel"


# Left out of the cockpit layout to keep it lean: herdr's files/changes side panel.
LEFT_OUT = {"files", "changes"}


def _without_mods(node: Dict, mods: Set[str]) -> Dict:
    """Drops blocks showing one of `mods`, and containers left empty by that."""
    if "children" in node:
        kept = [_without_mods(c, mods) for c in node["children"]
                if (c.get("content") or {}).get("mod") not in mods]
        node["children"] = [c for c in kept if "children" not in c or c["children"]]
    return node


def _swap_mod(node: Dict, old: str, new: str) -> None:
    if (node.get("content") or {}).get("mod") == old:
        node["content"]["mod"] = new
    for child in node.get("children") or []:
        _swap_mod(child, old, new)


def _add_below_mod(node: Dict, mod: str, block: Dict) -> bool:
    """Puts a copy of `block` right after the block showing `mod`, in the same container."""
    children = node.get("children") or []
    for at, child in enumerate(children):
        if (child.get("content") or {}).get("mod") == mod:
            children.insert(at + 1, json.loads(json.dumps(block)))
            return True
        if _add_below_mod(child, mod, block):
            return True
    return False


def install_menu_tabs(paths: Paths, backup: BackupStore) -> bool:
    """The tab bar opens herdr's "+" menu by name, so the cockpit layout gets a copy of the shipped tabs mod
    that opens ours. Generated at every install from what herdr ships; `menu_findings` says when an update of
    herdr left it behind. False if herdr changed that call: the layout then keeps herdr's own menu."""
    source = paths.herdr_mods / "tabs"
    try:
        text = (source / "main.js").read_text()
    except OSError:
        return False
    if text.count(TABS_OPENS_MENU) != 1:
        return False
    backup.write_text(paths.herdr_mods / "mesh-tabs" / "main.js", text.replace(TABS_OPENS_MENU, TABS_OPENS_OURS))
    backup.write_text(paths.herdr_mods / "mesh-tabs" / "mod.json", (source / "mod.json").read_text())
    return True


def menu_findings(paths: Paths) -> List[str]:
    """Our "+" menu and spaces list are copies of herdr's: when herdr changes an original, the copy needs a look."""
    findings = []
    installed = load_json(paths.herdr_app_home / "installed.json")
    for mod, upstream, finding in (
            ("new-menu", NEW_MENU_UPSTREAM,
             "Herdr's \"+\" menu has changed; our copy (mesh-new-menu) does not know that change yet"),
            ("spaces", SPACES_UPSTREAM,
             "Herdr's spaces list has changed; our copy (mesh-spaces) does not know that change yet")):
        shipped = installed.get(mod)
        version = shipped.get("main.js") if isinstance(shipped, dict) else None
        if version and version != upstream:
            findings.append(finding)
    try:
        tabs = (paths.herdr_mods / "tabs" / "main.js").read_text()
    except OSError:
        if main_layout_ready(paths):  # before herdr's first start there is nothing to miss yet
            findings.append("Herdr's tab bar is missing: the cockpit layout uses Herdr's own menu")
        return findings
    ours = paths.herdr_mods / "mesh-tabs" / "main.js"
    if tabs.count(TABS_OPENS_MENU) != 1:
        findings.append("Herdr's tab bar opens the \"+\" menu in a new way: the cockpit layout uses Herdr's own menu")
    elif not ours.is_file() or ours.read_text() != tabs.replace(TABS_OPENS_MENU, TABS_OPENS_OURS):
        findings.append("Herdr's tab bar has changed: moshi-cockpit install updates our copy (mesh-tabs)")
    return findings


def cockpit_layout(main: Dict, own_menu: bool = False) -> Dict:
    """herdr's shipped `main` layout with "Your agents" below Spaces and Agents, our spaces list (mesh-spaces)
    instead of herdr's and without the files/changes panel. Shipped layouts are read-only and replaced on every
    start, so ours is a separate file derived from the current one; `main` keeps herdr's spaces."""
    layout = _without_mods(json.loads(json.dumps(main)), LEFT_OUT)  # deep copy
    _swap_mod(layout, "spaces", "mesh-spaces")
    if own_menu:
        _swap_mod(layout, "tabs", "mesh-tabs")
    _add_below_mod(layout, "agents", OWN_AGENTS_BLOCK)
    layout["derived_from"] = "main (moshi-cockpit)"
    return layout


def main_layout_ready(paths: Paths) -> bool:
    """herdr writes its own `main` layout at its first start; ours is derived from it."""
    main = load_json(paths.herdr_app_home / "layouts" / "main.json")
    return main.get("type") == "row" and bool(main.get("children"))


def install_cockpit_layout(paths: Paths, backup: BackupStore) -> str:
    if not main_layout_ready(paths):
        return "Cockpit layout: skipped, Herdr has never run here. " + RERUN_HINT
    main = load_json(paths.herdr_app_home / "layouts" / "main.json")
    target = paths.herdr_app_home / "layouts" / f"{COCKPIT_LAYOUT}.json"
    own_menu = install_menu_tabs(paths, backup)
    layout = cockpit_layout(main, own_menu)
    changed = backup.write_text(target, json.dumps(layout, indent=2) + "\n")
    note = "Cockpit layout (Your agents in the sidebar): " + ("created or updated" if changed else "unchanged")
    if OWN_AGENTS_BLOCK["id"] not in json.dumps(layout):
        note += "; Your agents is missing, Herdr's sidebar has no Agents list any more"
    return note


TABS_OPENS_MENU = 'openOverlay({ mod: "new-menu", kind: "popup", width: 250, height: 512 })'
TABS_OPENS_OURS = 'openOverlay({ mod: "mesh-new-menu", kind: "popup", width: 250, height: 640 })'
NEW_MENU_UPSTREAM = "2f13095cb116c3cf"  # installed.json of the herdr-app our mesh-new-menu was copied from
SPACES_UPSTREAM = "bbcc823778bd7475"  # installed.json of the herdr-app our mesh-spaces was copied from
MOD_SUFFIXES = {".js", ".json", ".svg", ".png"}


def install_mods(paths: Paths, backup: BackupStore) -> List[str]:
    """Copies our herdr mods (share/herdr-mods/<name>/) into the app home. herdr reloads them on save."""
    changed = []
    source_root = SHARE / "herdr-mods"
    for mod_dir in sorted(p for p in source_root.iterdir() if p.is_dir()):
        # Only real mod files; herdr writes its own .d.ts and jsconfig.json into every mod folder.
        for source in sorted(f for f in mod_dir.iterdir()
                             if f.is_file() and f.suffix in MOD_SUFFIXES and f.name != "jsconfig.json"):
            target = paths.herdr_mods / mod_dir.name / source.name
            if backup.write_text(target, source.read_text()):
                changed.append(f"{mod_dir.name}/{source.name}")
    return changed


def ensure_claude_launcher(paths: Paths, backup: BackupStore, environ: Mapping[str, str] = os.environ) -> str:
    """herdr's own menu starts claude without the person's shell, so a wrapper defined there (a start menu)
    never runs. programs.json overrides that one field; anything the person set there stays."""
    from cockpit.launch import interactive_shell
    target = paths.herdr_app_home / "programs.json"
    shell = interactive_shell(environ)
    if not shell:
        return "Herdr menu, claude: unchanged (your shell is not zsh or bash)"
    if target.exists() and not load_json(target):
        return "Herdr menu, claude: skipped, programs.json is empty or not valid JSON (not overwritten)"
    data = load_json(target)
    programs = data.setdefault("programs", {})
    if not isinstance(programs, dict):
        return PROGRAMS_BROKEN
    entry = programs.setdefault("claude", {})
    ours = [shell, "-ic", 'claude "$@"', "claude"]  # arguments reach claude: the session list resumes with them
    if not isinstance(entry, dict) or ("run" in entry and entry["run"] not in (ours, [shell, "-ic", "claude"])):
        return "Herdr menu, claude: your own start command in programs.json stays"
    entry["run"] = ours
    changed = backup.write_text(target, json.dumps(data, indent=2) + "\n")
    return "Herdr menu, claude: starts through your shell" + ("" if changed else ", unchanged")


# Earlier releases shipped a Vibe icon under herdr's home ("mesh-icons/vibe.svg"); an entry pointing there is ours.
EARLIER_VIBE_ICON = "mesh-icons/vibe.svg"
VIBE_STANDARD = {"name": "Vibe", "run": ["vibe", "--agent", "ask"]}  # no icon: the package ships no images


def ensure_vibe_entry(paths: Paths, backup: BackupStore) -> str:
    """herdr ships no Vibe: this adds it to "Start a standard agent". An entry with the icon of an earlier release
    counts as ours and is updated; any other entry for it stays."""
    from cockpit.harnesses import HARNESSES
    target = paths.herdr_app_home / "programs.json"
    if target.exists() and not load_json(target):
        return "Herdr menu, Vibe: skipped, programs.json is empty or not valid JSON (not overwritten)"
    data = load_json(target)
    programs = data.setdefault("programs", {})
    if not isinstance(programs, dict):
        return PROGRAMS_BROKEN
    current = programs.get("vibe")
    if current == VIBE_STANDARD:
        return "Herdr menu, Vibe: unchanged"
    ours = isinstance(current, dict) and current.get("icon") == EARLIER_VIBE_ICON  # written by an earlier install
    if current is not None and not ours:
        return "Herdr menu, Vibe: your own entry in programs.json stays"
    if current is None and not HARNESSES["vibe"].installed(paths):
        return "Herdr menu, Vibe: skipped, vibe is not installed here"
    programs["vibe"] = dict(VIBE_STANDARD)
    backup.write_text(target, json.dumps(data, indent=2) + "\n")
    return "Herdr menu, Vibe: added under \"Start a standard agent\""


# herdr's own icons for the harnesses that have one; Vibe has none (herdr ships none, this package ships no images).
MENU_ICONS = {"claude": "icons/claude.svg", "codex": "icons/codex.svg", "grok": "icons/grok.svg",
              "agy": "icons/antigravity.svg"}
PROGRAMS_BROKEN = "Herdr menu: skipped, programs.json has an unexpected format (not overwritten)"
MENU_PREFIX = "moshi-cockpit-"  # our entries in programs.json: only these are ever removed


def sync_agent_menu(paths: Paths, backup: BackupStore) -> str:
    """One entry per agent in herdr's "+" menu: a click starts that agent through
    `moshi-cockpit run`, with its harness, role and identity. Entries of removed agents go, others stay."""
    from cockpit.config import load_config
    from cockpit.harnesses import HARNESSES
    from cockpit.registry import load_agents
    target = paths.herdr_app_home / "programs.json"
    data = load_json(target)
    if target.exists() and not data:
        return "Herdr menu, agents: skipped, programs.json is empty or not valid JSON (not overwritten)"
    config = load_config(paths)
    wanted = {}
    for agent in load_agents(paths, enforce=False):
        harness = HARNESSES[agent.harness]
        label = agent.name + (" (no moshi identity of its own)" if config.has("moshi") and "moshi" not in harness.mesh_servers else "")
        entry = {"name": label, "run": [str(paths.local_bin / "moshi-cockpit"), "run", agent.name]}
        if agent.harness in MENU_ICONS:
            entry["icon"] = MENU_ICONS[agent.harness]
        wanted[MENU_PREFIX + agent.name] = entry
    programs = data.setdefault("programs", {})
    if not isinstance(programs, dict):
        return PROGRAMS_BROKEN
    for key in [k for k in programs if k.startswith(MENU_PREFIX) and k not in wanted]:
        del programs[key]
    programs.update(wanted)
    changed = backup.write_text(target, json.dumps(data, indent=2) + "\n")
    return f"Herdr menu, agents: {len(wanted)} entries" + ("" if changed else ", unchanged")


def load_json(path: Path) -> Dict:
    if not path.is_file():
        return {}
    try:
        data = json.loads(path.read_text())
    except ValueError:
        return {}
    return data if isinstance(data, dict) else {}
