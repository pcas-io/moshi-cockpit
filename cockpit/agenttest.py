"""`moshi-cockpit test`: runs the agent test and restores each agent's `working_on` afterwards.

The test overwrites `working_on` (mesh_register ... idle). Without restoring it, an agent that
still holds a ball would look idle on the board (seen in a real run).
"""

from __future__ import annotations

import subprocess
from typing import Callable, Dict, List, Optional, Sequence

from cockpit.secrets import KeychainError, SecretStore
from cockpit.mcp import McpClient, McpError
from cockpit.config import load_config, require
from cockpit.tokens import moshi_token
from cockpit.paths import BIN, keychain_service
from cockpit.paths import Paths
from cockpit.status import fetch_agents, pick_identity

Runner = Callable[..., "subprocess.CompletedProcess[str]"]


def snapshot(keychain: SecretStore, agents: Sequence[str], identity: str, url: str, paths: Paths) -> Dict[str, str]:
    """working_on per local agent, looked up under its moshi name (the agent's name)."""
    current = {a.get("name"): a.get("working_on") or "idle" for a in fetch_agents(keychain, identity, url, paths)}
    return {agent: current.get(agent, "idle") for agent in agents}


def restore(keychain: SecretStore, before: Dict[str, str], url: str, paths: Paths,
            client_factory: Callable[[str, str], McpClient] = McpClient) -> List[str]:
    notes = []
    for agent, working_on in before.items():
        try:
            token = moshi_token(paths, keychain, agent, url)
            client_factory(url, token).call_tool("mesh_register", {"working_on": working_on})
            notes.append(f"{agent}: working_on wiederhergestellt")
        except (KeychainError, McpError, ValueError) as exc:
            notes.append(f"{agent}: working_on NICHT wiederhergestellt ({exc})")
    return notes


def run(keychain: SecretStore, agents: Sequence[str], report_to: str, runner: Runner = subprocess.run,
        paths: Optional[Paths] = None) -> int:
    paths = paths or Paths.from_env()
    config = load_config(paths)
    url = require(config, "moshi")
    identity = pick_identity(keychain, None, agents)
    before = snapshot(keychain, agents, identity, url, paths)
    result = runner([str(BIN / "mesh-agent-test"), report_to, *agents], check=False)
    for note in restore(keychain, before, url, paths):
        print(note)
    return result.returncode
