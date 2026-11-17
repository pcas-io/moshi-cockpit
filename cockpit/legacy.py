"""Moving an earlier setup (`~/.config/mesh-cockpit/`) onto moshi-cockpit, in two steps.

1. `install` runs `import_config` once: the earlier setup's changes to Herdr are undone first (Herdr's own
   files are back before the new install writes its own), then servers, agents, rules and the person's
   ocellio token are carried over. The old registration token is removed here, before `connect` stores the
   new one under the same Keychain name. The old config folder itself is only read, never changed.
2. `moshi-cockpit migrate`, right after `connect`, adopts each agent's older token for this installation.
   The token itself stays the same, so its Keychain entry stays as it is; the old helper entries go.
   Running it again only reports what was moved already.

The old agent entries in the Keychain use the names moshi-cockpit uses, so they are never touched here.
No token is printed.
"""

from __future__ import annotations

import json
import shutil
from dataclasses import replace
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

from cockpit.agents import AgentContext
from cockpit.agents_remote import APPROVAL, ApprovalRequired, refusal
from cockpit.backup import BackupStore
from cockpit.config import SERVERS, load_config, origin, read_private, require, save_rules, validate_url
from cockpit.connect import OCELLIO_ENTRY, REGISTER_ENTRY, approval_valid
from cockpit.harnesses import HARNESSES
from cockpit.moshi_api import MoshiApi, MoshiError
from cockpit.paths import Paths, keychain_service
from cockpit.registry import Agent, _validate, load_agents, save_agents
from cockpit.secrets import HardenedStore, KeychainError, SecretStore
from cockpit.tokens import OCELLIO_DEFAULT, REGISTER, bound_origin, moshi_token

FOUND = 'Found an earlier setup. After "Connect to moshi", run: moshi-cockpit migrate'
REVOKE_HINT = "Old helper identities can be revoked in the moshi dashboard."
# Where the earlier setup bound the person's ocellio token (entry `ocellio-cockpit` / `cockpit`).
OLD_OCELLIO_BINDING = "cockpit-ocellio"
OLD_OCELLIO_ENTRY = (keychain_service("ocellio", "cockpit"), "cockpit")
OLD_HELPER_ENTRIES = ((keychain_service("moshi", "cockpit"), "cockpit"), (keychain_service("moshi", "admin"), "admin"),
                      OLD_OCELLIO_ENTRY)
# Written once the import is complete. It lives in the state folder, which `uninstall` keeps, so a later
# install after an uninstall does not carry the earlier setup over a second time.
IMPORTED_MARKER = "earlier-setup-imported"
REFUSED = {"not_found": "moshi does not know its token", "agent_inactive": "the agent is switched off in moshi",
           "invalid_token": "moshi refused its token", "admin_token_refused": "moshi refused its token",
           "invalid_profile": "moshi does not accept its harness"}


def old_config_dir(paths: Paths) -> Path:
    return paths.config / "mesh-cockpit"


def old_state_dir(paths: Paths) -> Path:
    return paths.home / ".local" / "state" / "mesh-cockpit"


def old_agent_home(paths: Paths, name: str) -> Path:
    return paths.home / ".local" / "share" / "mesh-cockpit" / "agents" / name


# -- step 1: during install ----------------------------------------------------------------------


def import_config(paths: Paths, backup: BackupStore, keychain: SecretStore) -> List[str]:
    """Once, when an earlier setup exists and moshi-cockpit has neither a config nor a finished import.
    Everything is read and checked before anything changes (an old file others can read stops the import,
    fail-closed). config.json is written last: an import that stopped before it simply runs again."""
    old, marker = old_config_dir(paths), paths.state / IMPORTED_MARKER
    if not old.is_dir() or paths.mesh_config.exists() or paths.mesh_config.is_symlink() or marker.exists():
        return []
    settings, old_ocellio = _old_settings(old / "config.json")
    agents, notes = _old_agents(paths, old / "agents.json")
    rules = _old_text(old / "rules.md", "rules")
    notes[:0] = _undo_old_changes(paths)
    if agents is not None:
        save_agents(paths, backup, agents)
    if rules:
        try:
            save_rules(paths, backup, rules)
        except ValueError as exc:
            notes.append(f"Earlier setup: rules not carried over ({exc})")
    carried = _carry_ocellio(keychain, settings["servers"].get("ocellio"), old_ocellio)
    if carried:
        settings["bound"] = {OCELLIO_DEFAULT: carried}
    if keychain.exists(*REGISTER_ENTRY):  # the earlier, unscoped one: connect stores a new one
        keychain.delete(*REGISTER_ENTRY)
    backup.write_text(paths.mesh_config, json.dumps(settings, indent=2) + "\n", mode=0o600)
    paths.state.mkdir(parents=True, exist_ok=True)
    marker.write_text("1\n")
    if carried and keychain.exists(*OLD_OCELLIO_ENTRY):
        keychain.delete(*OLD_OCELLIO_ENTRY)
    servers = ", ".join(sorted(settings["servers"])) or "no servers"
    notes.append(f"Earlier setup carried over: {servers}, {len(agents or [])} agent(s)" + (", rules" if rules else "")
                 + (", your ocellio token" if carried else ""))
    return notes


def _undo_old_changes(paths: Paths) -> List[str]:
    """The earlier setup's uninstall, entry by entry, newest first, for everything outside its config folder
    (those files are what is carried over). An entry whose backup file is gone is skipped before anything
    is removed, so a stale list never deletes a file of Herdr's. The old list is cleared at the end."""
    manifest = old_state_dir(paths) / "backups.json"
    if not manifest.is_file():
        return []
    try:
        entries = json.loads(manifest.read_text())
    except ValueError:
        return ["Earlier setup: its list of changes is unreadable, nothing undone"]
    if not isinstance(entries, list):
        return ["Earlier setup: its list of changes is unreadable, nothing undone"]
    undone, skipped, keep = 0, [], old_config_dir(paths)
    for entry in reversed(entries):
        path = Path(str(entry.get("path") or "")) if isinstance(entry, dict) else None
        if path is None or not path.is_absolute() or path == keep or keep in path.parents:
            continue
        if _undo(path, entry.get("backup")):
            undone += 1
        else:
            skipped.append(str(path))
    manifest.write_text("[]")
    notes = [f"Earlier setup: {undone} change(s) undone"] if undone else []
    if skipped:
        notes.append("Earlier setup: left as they are, their backup is missing or cannot be put back: "
                     + ", ".join(skipped))
    return notes


def _undo(path: Path, backup: Any) -> bool:
    """One entry as BackupStore records it: None (the file was new), "symlink:<target>" or a backup file."""
    if backup is not None and not isinstance(backup, str):
        return False
    restore = backup is not None and not backup.startswith("symlink:")
    if restore and not Path(backup).is_file():
        return False
    try:
        if path.is_symlink() or path.is_file():
            path.unlink()
        elif path.exists():
            return False  # never a folder
        if backup is not None and backup.startswith("symlink:"):
            path.symlink_to(backup[len("symlink:"):])
        elif restore:
            shutil.move(backup, path)
    except OSError:
        return False
    return True


def _carry_ocellio(keychain: SecretStore, url: Optional[str], old_origin: Optional[str]) -> Optional[str]:
    """The person's old ocellio token becomes `ocellio-default`, only for the server it was given for and
    with the same strictness. Returns the origin to bind it to, or None when nothing was carried over.
    An `ocellio-default` that is already there is bound only when it is that same token (an import that
    stopped half-way wrote it). The Keychain refusing (Deny at its prompt) carries nothing over: connect
    asks for the token later."""
    try:
        if not url or old_origin != origin(url) or not keychain.exists(*OLD_OCELLIO_ENTRY):
            return None
        token = keychain.read(*OLD_OCELLIO_ENTRY)
        if keychain.exists(*OCELLIO_ENTRY):
            return old_origin if keychain.read(*OCELLIO_ENTRY) == token else None
        strict = isinstance(keychain, HardenedStore) and keychain.is_strict(*OLD_OCELLIO_ENTRY)
        keychain.write(*OCELLIO_ENTRY, token, strict=strict)
    except KeychainError:
        return None
    return old_origin


def _old_text(path: Path, what: str) -> str:
    if not path.exists() and not path.is_symlink():
        return ""
    return read_private(path, f"earlier setup, {what}")


def _old_json(path: Path, what: str) -> Optional[Dict[str, Any]]:
    text = _old_text(path, what)
    if not text:
        return None
    try:
        data = json.loads(text)
    except ValueError:
        raise ValueError(f"{path} is not valid JSON; nothing was carried over") from None
    if not isinstance(data, dict):
        raise ValueError(f"{path} has an unexpected format; nothing was carried over")
    return data


def _old_settings(path: Path) -> Tuple[Dict[str, Any], Optional[str]]:
    """The servers, and the origin the old ocellio token was bound to. The person, every other binding and
    the old migration marker stay behind."""
    data = _old_json(path, "settings") or {}
    raw = data.get("servers") if isinstance(data.get("servers"), dict) else {}
    settings: Dict[str, Any] = {"servers": {name: validate_url(str(raw[name])) for name in SERVERS if raw.get(name)}}
    bound = data.get("bound") if isinstance(data.get("bound"), dict) else {}
    old_ocellio = bound.get(OLD_OCELLIO_BINDING)
    return settings, old_ocellio if isinstance(old_ocellio, str) else None


def _old_agents(paths: Paths, path: Path) -> Tuple[Optional[List[Agent]], List[str]]:
    """The agents as the new rules accept them; an entry they refuse stays behind with one note. An agent's
    folder of the earlier setup (a Vibe profile with its sessions, say) stays where it is and is used."""
    data = _old_json(path, "agents")
    if data is None:
        return None, []
    items = data.get("agents")
    if not isinstance(items, list):
        raise ValueError(f"{path} has an unexpected format; nothing was carried over")
    agents: List[Agent] = []
    notes: List[str] = []
    for raw in items:
        try:
            agent = _validate(raw, list(HARNESSES))
        except ValueError as exc:
            notes.append(f"Earlier setup: an agent was not carried over ({exc})")
            continue
        if any(a.name.lower() == agent.name.lower() for a in agents):
            notes.append(f"Earlier setup: agent '{agent.name}' appears twice, the second was not carried over")
            continue
        agents.append(_with_old_home(paths, agent))
    return agents, notes


def _with_old_home(paths: Paths, agent: Agent) -> Agent:
    """The earlier setup kept a harness's files in `<agent folder>/<harness>` (Vibe's VIBE_HOME)."""
    folder = old_agent_home(paths, agent.name)
    if agent.home or not folder.is_dir():
        return agent
    own = folder / agent.harness
    return replace(agent, home=str(own if own.is_dir() else folder))


# -- step 2: `moshi-cockpit migrate`, right after connect ----------------------------------------


def _print_note(note: str) -> None:
    print(f"• {note}")


def migrate(ctx: AgentContext, api: MoshiApi, say: Callable[[str], None] = _print_note) -> List[str]:
    """Adopts every local agent's older token for this installation, then removes the old helper entries
    (only when no agent was refused: a refused one may still need them). Needs a valid approval
    (ApprovalRequired, exit 3, otherwise or when moshi refuses it). When moshi stops answering half-way,
    what was moved so far is told with `say` before the error goes up."""
    config = load_config(ctx.paths)
    url = require(config, "moshi")
    if not approval_valid(ctx.paths, ctx.keychain) or bound_origin(ctx.paths, REGISTER) != origin(url):
        raise ApprovalRequired(APPROVAL)
    registration = moshi_token(ctx.paths, ctx.keychain, REGISTER, url)
    notes: List[str] = []
    refused = False
    for agent in load_agents(ctx.paths, enforce=False):
        if not ctx.keychain.exists(keychain_service("moshi", agent.name), agent.name):
            continue
        try:
            note, moved = _adopt(ctx, api, registration, agent, url)
        except (ApprovalRequired, ValueError):
            for done in notes:
                say(done)
            raise
        notes.append(note)
        refused = refused or not moved
    if refused:
        notes.append("The earlier helper entries stay in the Keychain until every agent is moved")
    else:
        removed = [service for service, account in OLD_HELPER_ENTRIES if _delete(ctx.keychain, service, account)]
        if removed:
            notes.append("Removed the earlier helper entries from the Keychain (" + ", ".join(removed) + ")")
    return (notes or ["Nothing to move"]) + [REVOKE_HINT]


def _adopt(ctx: AgentContext, api: MoshiApi, registration: str, agent: Agent, url: str) -> Tuple[str, bool]:
    """One line for the agent, and whether it is held by this installation now."""
    try:
        token = moshi_token(ctx.paths, ctx.keychain, agent.name, url)  # only for the origin it is bound to
    except ValueError:
        return f"{agent.name}: not moved, its token is not bound to the configured moshi (nothing sent)", False
    try:
        api.adopt(registration, token, agent.harness)
    except MoshiError as exc:
        if exc.status == 401:
            raise ApprovalRequired(APPROVAL) from None
        if exc.reason == "already_adopted":
            return f"{agent.name}: already moved", True
        if exc.reason in REFUSED:
            return f"{agent.name}: not moved, {REFUSED[exc.reason]}", False
        raise refusal(exc, agent.name) from None
    return f"{agent.name}: moved to this installation", True


def _delete(keychain: SecretStore, service: str, account: str) -> bool:
    if not keychain.exists(service, account):
        return False
    keychain.delete(service, account)
    return True
