"""Moving an earlier setup (`~/.config/mesh-cockpit/`) onto moshi-cockpit, in two steps.

1. `install` runs `import_config` once: the old uninstall's backups go back first (Herdr's own files are
   restored before the new install writes its own), then servers, agents and rules are copied. The old
   registration token is removed here, before `connect` stores the new one under the same Keychain name.
2. `moshi-cockpit migrate`, right after `connect`, adopts each agent's older token for this installation.
   The token itself stays the same, so its Keychain entry stays as it is. The person's old ocellio token is
   carried over and the old helper entries go. Running it again only reports what was moved already.

The old agent entries in the Keychain use the names moshi-cockpit uses, so they are never touched here.
No token is printed.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from cockpit.agents import AgentContext
from cockpit.agents_remote import APPROVAL, ApprovalRequired, refusal
from cockpit.backup import BackupStore
from cockpit.config import (SERVERS, load_config, origin, read_private, require, save_rules, setting, update,
                            validate_url)
from cockpit.connect import OCELLIO_ENTRY, REGISTER_ENTRY, approval_valid
from cockpit.harnesses import HARNESSES
from cockpit.moshi_api import MoshiApi, MoshiError
from cockpit.paths import Paths, keychain_service
from cockpit.registry import Agent, _validate, load_agents, save_agents
from cockpit.secrets import SecretStore
from cockpit.tokens import OCELLIO_DEFAULT, REGISTER, bind_identity, bound_origin, moshi_token

FOUND = 'Found an earlier setup. After "Connect to moshi", run: moshi-cockpit migrate'
REVOKE_HINT = "Old helper identities can be revoked in the moshi dashboard."
# Where the earlier setup bound the person's ocellio token; kept by the import until `migrate` used it.
OLD_OCELLIO_BINDING = "cockpit-ocellio"
OLD_OCELLIO_ENTRY = (keychain_service("ocellio", "cockpit"), "cockpit")
OLD_HELPER_ENTRIES = ((keychain_service("moshi", "cockpit"), "cockpit"), (keychain_service("moshi", "admin"), "admin"))
REFUSED = {"not_found": "moshi does not know its token", "agent_inactive": "the agent is switched off in moshi",
           "invalid_token": "moshi refused its token", "admin_token_refused": "moshi refused its token",
           "invalid_profile": "moshi does not accept its harness"}


def old_config_dir(paths: Paths) -> Path:
    return paths.config / "mesh-cockpit"


def old_state_dir(paths: Paths) -> Path:
    return paths.home / ".local" / "state" / "mesh-cockpit"


# -- step 1: during install ----------------------------------------------------------------------


def import_config(paths: Paths, backup: BackupStore, keychain: SecretStore) -> List[str]:
    """Once, when an earlier setup exists and moshi-cockpit has no config folder yet. Everything is read and
    checked before anything changes (an old file others can read stops the import, fail-closed)."""
    old = old_config_dir(paths)
    if not old.is_dir() or paths.mesh_config.parent.exists():
        return []
    settings = _old_settings(old / "config.json")
    agents, notes = _old_agents(old / "agents.json")
    rules = _old_text(old / "rules.md", "rules")
    notes[:0] = _restore_old_backups(paths)
    backup.write_text(paths.mesh_config, json.dumps(settings, indent=2) + "\n", mode=0o600)
    if agents is not None:
        save_agents(paths, backup, agents)
    if rules:
        try:
            save_rules(paths, backup, rules)
        except ValueError as exc:
            notes.append(f"Earlier setup: rules not carried over ({exc})")
    if keychain.exists(*REGISTER_ENTRY):  # the earlier, unscoped one: connect stores a new one
        keychain.delete(*REGISTER_ENTRY)
    servers = ", ".join(sorted(settings["servers"])) or "no servers"
    notes.append(f"Earlier setup carried over: {servers}, {len(agents or [])} agent(s)" + (", rules" if rules else ""))
    return notes


def _restore_old_backups(paths: Paths) -> List[str]:
    """The old uninstall. A broken backup list does not block the move: one note, and the new install's
    own files replace the old ones where they overlap."""
    try:
        restored = BackupStore(old_state_dir(paths)).restore_all()
    except (OSError, ValueError) as exc:
        return [f"Earlier setup: not every change could be restored ({type(exc).__name__})"]
    return [f"Earlier setup: {len(restored)} change(s) restored to Herdr's originals"] if restored else []


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


def _old_settings(path: Path) -> Dict[str, Any]:
    """The servers, and the old ocellio binding for `migrate`. The person, the old helper and registration
    bindings and the old migration marker are dropped."""
    data = _old_json(path, "settings") or {}
    raw = data.get("servers") if isinstance(data.get("servers"), dict) else {}
    settings: Dict[str, Any] = {"servers": {name: validate_url(str(raw[name])) for name in SERVERS if raw.get(name)}}
    bound = data.get("bound") if isinstance(data.get("bound"), dict) else {}
    if isinstance(bound.get(OLD_OCELLIO_BINDING), str):
        settings["bound"] = {OLD_OCELLIO_BINDING: bound[OLD_OCELLIO_BINDING]}
    return settings


def _old_agents(path: Path) -> Tuple[Optional[List[Agent]], List[str]]:
    """The agents as the new rules accept them; an entry they refuse stays behind with one note."""
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
        agents.append(agent)
    return agents, notes


# -- step 2: `moshi-cockpit migrate`, right after connect ----------------------------------------


def migrate(ctx: AgentContext, api: MoshiApi) -> List[str]:
    """Adopts every local agent's older token for this installation, then cleans up the old helper entries.
    Needs a valid approval (ApprovalRequired, exit 3, otherwise or when moshi refuses it)."""
    config = load_config(ctx.paths)
    url = require(config, "moshi")
    if not approval_valid(ctx.paths, ctx.keychain) or bound_origin(ctx.paths, REGISTER) != origin(url):
        raise ApprovalRequired(APPROVAL)
    registration = moshi_token(ctx.paths, ctx.keychain, REGISTER, url)
    notes = [_adopt(ctx, api, registration, agent, url) for agent in load_agents(ctx.paths, enforce=False)
             if ctx.keychain.exists(keychain_service("moshi", agent.name), agent.name)]
    notes.extend(_carry_ocellio(ctx, config))
    removed = [service for service, account in OLD_HELPER_ENTRIES if _delete(ctx.keychain, service, account)]
    if removed:
        notes.append("Removed the earlier helper entries from the Keychain (" + ", ".join(removed) + ")")
    return (notes or ["Nothing to move"]) + [REVOKE_HINT]


def _adopt(ctx: AgentContext, api: MoshiApi, registration: str, agent: Agent, url: str) -> str:
    try:
        token = moshi_token(ctx.paths, ctx.keychain, agent.name, url)  # only for the origin it is bound to
    except ValueError:
        return f"{agent.name}: not moved, its token is not bound to the configured moshi (nothing sent)"
    try:
        api.adopt(registration, token, agent.harness)
    except MoshiError as exc:
        if exc.status == 401:
            raise ApprovalRequired(APPROVAL) from None
        if exc.reason == "already_adopted":
            return f"{agent.name}: already moved"
        if exc.reason in REFUSED:
            return f"{agent.name}: not moved, {REFUSED[exc.reason]}"
        raise refusal(exc, agent.name) from None
    return f"{agent.name}: moved to this installation"


def _carry_ocellio(ctx: AgentContext, config) -> List[str]:
    """The person's old ocellio token becomes `ocellio-default` unless connect stored one, and only for the
    server it was given for. The old entry and its binding go either way."""
    old_origin = (setting(ctx.paths, "bound") or {}).get(OLD_OCELLIO_BINDING)
    notes: List[str] = []
    if ctx.keychain.exists(*OLD_OCELLIO_ENTRY):
        if ctx.keychain.exists(*OCELLIO_ENTRY):
            notes.append("ocellio: the token from connect stays")
        elif config.has("ocellio") and old_origin == origin(config.servers["ocellio"]):
            bind_identity(ctx.paths, ctx.backup, OCELLIO_DEFAULT, config.servers["ocellio"])
            ctx.keychain.write(*OCELLIO_ENTRY, ctx.keychain.read(*OLD_OCELLIO_ENTRY))
            notes.append("ocellio: your earlier token carried over")
        else:
            notes.append("ocellio: the earlier token was not for the configured server: run moshi-cockpit connect")
        ctx.keychain.delete(*OLD_OCELLIO_ENTRY)
    if old_origin is not None:
        bound = dict(setting(ctx.paths, "bound") or {})
        bound.pop(OLD_OCELLIO_BINDING, None)
        update(ctx.paths, ctx.backup, "bound", bound)
    return notes


def _delete(keychain: SecretStore, service: str, account: str) -> bool:
    if not keychain.exists(service, account):
        return False
    keychain.delete(service, account)
    return True
