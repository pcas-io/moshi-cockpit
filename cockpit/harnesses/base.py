"""What every harness (agent CLI) has to answer, so that agents are not tied to one CLI.

A harness knows how to start its CLI for a named agent (argv and environment, tokens only in the
environment), how to wire the configured servers into it, how to resume a session, where its sessions
live and what `doctor` should look at. Agents are data (registry.py); harnesses are code.
"""

from __future__ import annotations

import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Optional, Sequence, Tuple

from cockpit.backup import BackupStore
from cockpit.config import Config, origin
from cockpit.secrets import SecretStore
from cockpit.paths import Paths, keychain_service
from cockpit.presence import presence_text
from cockpit.registry import Agent

# Environment names the harness configs refer to. Fixed per process, so they need no agent in the name.
TOKEN_ENV = {"moshi": "MESH_MOSHI_TOKEN", "ocellio": "MESH_OCELLIO_TOKEN"}
# Prefixes dropped from an agent's environment before it starts (no herdr/hc control, no foreign
# sockets); SSH_AUTH_SOCK as well. Not isolation: the agent runs as the same user
# and can still read that user's files.
DROPPED_ENV_PREFIXES = ("HC_", "HERDR_", "CLAUDE_CODE_")
DROPPED_ENV = ("SSH_AUTH_SOCK",)
# Token variables are dropped for every harness: a child gets its own token and no one else's
# whatever the parent environment held.
TOKEN_ENV_PREFIXES = ("MESH_", "MOSHI_", "OCELLIO_")


@dataclass(frozen=True)
class Ctx:
    paths: Paths
    config: Config
    keychain: SecretStore
    backup: Optional[BackupStore] = None


class Harness:
    name = ""
    binary = ""
    # One agent of this harness per machine, and why (shown wherever a second one is refused).
    single_instance = False
    single_reason = ""
    # Wired to the servers per agent (own token).
    native_mesh = True
    # Which configured servers get this agent's own token.
    mesh_servers: Sequence[str] = ("ocellio", "moshi")
    # Drop herdr/hc/SSH references from the environment (see DROPPED_ENV_PREFIXES).
    clean_env = True
    resume_flag: Sequence[str] = ()
    # Arguments that keep asking on, whatever the CLI's own config says (person model, no auto-approve).
    permission_args: Sequence[str] = ()
    # Start through the person's interactive shell, so a wrapper defined there (a start menu) still runs.
    shell_start = False

    # Why this harness cannot get agents of its own; empty when it can.
    no_own_agents = ""

    def installed(self, paths: Paths) -> bool:
        return bool(shutil.which(self.binary)) or (paths.local_bin / self.binary).exists()

    def moshi_name(self, ctx: Ctx, agent: Agent) -> str:
        return agent.name

    def presence(self, ctx: Ctx, agent: Agent) -> str:
        return presence_text(self.moshi_name(ctx, agent), ctx.config, agent.role, agent.skill)

    def servers(self, ctx: Ctx) -> List[str]:
        """The configured servers this harness wires in for an agent."""
        return [s for s in self.mesh_servers if ctx.config.has(s)]

    def token_services(self, ctx: Ctx, agent: Agent) -> List[str]:
        return [keychain_service(s, agent.name) for s in self.servers(ctx)]

    def origin_mismatches(self, ctx: Ctx, agent: Agent) -> List[str]:
        """Servers whose configured origin is not the one the agent's token was issued for."""
        return [s for s in self.servers(ctx) if agent.bound.get(s) != origin(ctx.config.servers[s])]

    def tokens(self, ctx: Ctx, agent: Agent) -> Dict[str, str]:
        """Fail-closed: no token leaves the keychain for a server whose origin changed since binding, and
        an agent without its token does not start unbound (one agent, one identity)."""
        missing = self.missing_tokens(ctx, agent)
        if missing:
            raise ValueError(f"{agent.name} hat noch keine eigene Anbindung ({', '.join(missing)}). "
                             f"Einmal einrichten: moshi-cockpit bot add {agent.name}")
        wrong = self.origin_mismatches(ctx, agent)
        if wrong:
            details = ", ".join(f"{s}: gebunden an {agent.bound.get(s) or '–'}, Config {origin(ctx.config.servers[s])}"
                                for s in wrong)
            raise ValueError(f"{agent.name}: Server-Origin passt nicht zum Token ({details}); kein Token gesetzt. "
                             f"Absichtlich? moshi-cockpit bot add {agent.name} --rebind")
        return {s: ctx.keychain.read(keychain_service(s, agent.name), agent.name) for s in self.servers(ctx)}

    def missing_tokens(self, ctx: Ctx, agent: Agent) -> List[str]:
        return [svc for svc in self.token_services(ctx, agent) if not ctx.keychain.exists(svc, agent.name)]

    def setup(self, ctx: Ctx, agent: Agent) -> List[str]:
        """Writes the files this agent needs (through the backup store). Default: none."""
        return []

    def command(self, ctx: Ctx, agent: Agent, extra: Sequence[str]) -> Tuple[List[str], Dict[str, str]]:
        """argv and the extra environment for a start. Tokens go into the environment only."""
        env = {TOKEN_ENV[s]: token for s, token in self.tokens(ctx, agent).items()}
        return [self.binary, *self.permission_args, *self.args(ctx, agent), *self.map_extra(extra)], env

    def args(self, ctx: Ctx, agent: Agent) -> List[str]:
        return []

    def map_extra(self, extra: Sequence[str]) -> List[str]:
        return list(extra)

    def resume_args(self, session_id: str) -> List[str]:
        return [*self.resume_flag, session_id]

    def session_home(self, ctx: Ctx, agent: Agent) -> Optional[Path]:
        return None

    def findings(self, ctx: Ctx, agent: Agent) -> List[str]:
        return []
