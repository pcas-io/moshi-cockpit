"""Tool policy for the local agents, the same for Vibe, agy and Codex.

- ALWAYS: reading, reports (save_entity) and the mesh; no question.
- ASK: everything that changes or reveals more; the user confirms in the agent's tab. Without a
  user interface (headless runs, the agent test) the agents refuse on their own (verified by hand:
  Vibe "ask", agy "headless mode cannot prompt … auto-denied", Codex "approval policy is never").
- IRREVERSIBLE: a subset of ASK that the presence rule makes the agent announce before asking.

An agent's moshi name is its local name; there is no per-user prefix.
"""

from __future__ import annotations

from typing import Dict

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
