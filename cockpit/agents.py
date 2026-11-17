"""Setting up the person's agents, independent of any one agent CLI: the harness does the CLI-specific work,
this module ties it to the registry, the config and the keychain."""

from __future__ import annotations

from dataclasses import dataclass
from typing import List

from cockpit.backup import BackupStore
from cockpit.config import load_config
from cockpit.harnesses import HARNESSES, Ctx
from cockpit.harnesses.base import CONNECT_HINT, MOVED_HINT
from cockpit.secrets import SecretStore
from cockpit.paths import BIN, Paths
from cockpit.registry import Agent

# Our commands on PATH.
LAUNCHERS = ("moshi-cockpit",)


@dataclass(frozen=True)
class AgentContext:
    paths: Paths
    backup: BackupStore
    keychain: SecretStore


def harness_ctx(ctx: AgentContext) -> Ctx:
    return Ctx(paths=ctx.paths, config=load_config(ctx.paths), keychain=ctx.keychain, backup=ctx.backup)


def link_launchers(ctx: AgentContext) -> List[str]:
    changed = []
    for name in LAUNCHERS:
        if ctx.backup.symlink(BIN / name, ctx.paths.local_bin / name):
            changed.append(name)
    return changed


def setup(ctx: AgentContext, agent: Agent) -> List[str]:
    """Writes the agent's files. Needs its tokens for the configured servers in the keychain."""
    hctx = harness_ctx(ctx)
    harness = HARNESSES[agent.harness]
    missing = harness.missing_tokens(hctx, agent)
    if missing:
        raise ValueError(f"{agent.name}: Keychain entries missing ({', '.join(missing)}): {CONNECT_HINT}")
    return harness.setup(hctx, agent)


def findings(ctx: AgentContext, agent: Agent) -> List[str]:
    """doctor's wording; doctor._agent_action matches on it."""
    hctx = harness_ctx(ctx)
    harness = HARNESSES[agent.harness]
    found = [f"Keychain entry missing: {svc}" for svc in harness.missing_tokens(hctx, agent)]
    for server in harness.origin_mismatches(hctx, agent):
        if agent.bound.get(server):
            found.append(f"Origin mismatch {server}: token bound to {agent.bound[server]}, the config points to "
                         f"{hctx.config.servers[server]}; no token is set ({MOVED_HINT})")
        else:
            found.append(f"{server} token not bound to a server yet; no token is set ({CONNECT_HINT})")
    if not harness.installed(ctx.paths):
        found.append(f"{harness.binary} not installed")
    return found + harness.findings(hctx, agent)
