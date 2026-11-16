"""Who the local agents act for (the person's rights, each agent's own name).

In ocellio a local agent uses the person's own token and rights; writing asks instead of being blocked.
In moshi every agent keeps a name of its own, `<person>-<agent>` (e.g. `anna-vibe`), so the mesh shows
who wrote what and who is live. Without a person set the moshi name is the agent's name.

Tool policy, the same for Vibe, agy and Codex:
- ALWAYS: reading, reports (save_entity) and the mesh; no question.
- ASK: everything that changes or reveals more; the person confirms in the agent's tab. Without a
  user interface (headless runs, the agent test) the agents refuse on their own (verified by hand:
  Vibe "ask", agy "headless mode cannot prompt … auto-denied", Codex "approval policy is never").
- IRREVERSIBLE: a subset of ASK that the presence rule makes the agent announce before asking.
"""

from __future__ import annotations

import json
import re
from typing import Dict, Optional

from cockpit.backup import BackupStore
from cockpit.paths import Paths

OCELLIO_ALWAYS = ["context_load", "search_entities", "get_entity", "get_related", "save_entity", "list_entities",
                  "list_kinds", "list_relations", "list_skills", "load_skill", "list_proposals", "ping"]
OCELLIO_ASK = ["update_entity", "archive_entity", "link_entities", "unlink_entity", "decide_proposal",
               "connect_repo", "auth_events", "lint_graph"]
MOSHI_ALWAYS = ["mesh_register", "mesh_receive", "mesh_inbox", "mesh_status", "mesh_get", "mesh_history",
                "mesh_send", "mesh_reply"]
MOSHI_ASK = ["mesh_webhook_set", "mesh_webhook_get", "mesh_webhook_delete"]
IRREVERSIBLE = ["archive_entity", "unlink_entity", "decide_proposal", "connect_repo", "mesh_webhook_set",
                "mesh_webhook_delete"]

ALWAYS: Dict[str, list] = {"ocellio": OCELLIO_ALWAYS, "moshi": MOSHI_ALWAYS}
ASK: Dict[str, list] = {"ocellio": OCELLIO_ASK, "moshi": MOSHI_ASK}

# moshi's own rule for agent names; the person part leaves room for "-<agent>".
_PERSON_RE = re.compile(r"[a-z0-9][a-z0-9_-]{0,40}")


def validate_person(name: str) -> str:
    name = (name or "").strip().lower()
    if not _PERSON_RE.fullmatch(name):
        raise ValueError("Ungültiger Personenname (a-z, 0-9, _ und -, höchstens 41 Zeichen)")
    return name


def load_person(paths: Paths) -> Optional[str]:
    """None only when there is no config or it names no person. A config that exists but cannot be
    read or names an invalid person is an error (fail-closed): falling back to the plain names would
    give another person's agents the plain names, i.e. someone else's identities."""
    from cockpit.config import _read  # one reader: permission check, JSON, format
    data = _read(paths)
    person = data.get("person")
    if person in (None, ""):
        return None
    try:
        return validate_person(str(person))
    except ValueError:
        raise ValueError(f"{paths.mesh_config}: ungültiger Personenname; moshi-cockpit install --person <name>") from None


def save_person(paths: Paths, backup: BackupStore, name: str) -> bool:
    person = validate_person(name)
    try:
        data = json.loads(paths.mesh_config.read_text())
    except (OSError, ValueError):
        data = {}  # rewriting a broken config with a valid person is the documented way out
    if not isinstance(data, dict):
        data = {}
    data["person"] = person
    return backup.write_text(paths.mesh_config, json.dumps(data, indent=2) + "\n", mode=0o600)


def identity(agent: str, person: Optional[str]) -> str:
    """The agent's moshi name: `<person>-<agent>`, or the plain agent name without a person."""
    return f"{person}-{agent}" if person else agent
