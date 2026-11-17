"""Mistral Vibe: one profile (VIBE_HOME) per agent, so several Vibe agents can live side by side."""

from __future__ import annotations

from pathlib import Path
from typing import List, Optional

from cockpit.harnesses.base import TOKEN_ENV, Ctx, Harness
from cockpit.policy import ALWAYS, ASK
from cockpit.registry import Agent


def vibe_config(servers: dict) -> str:
    """The whole profile, generated from the configured servers. doctor compares byte for byte."""
    out = ['# Vibe profile of a moshi-cockpit agent. Written by moshi-cockpit; start it only with `moshi-cockpit run`.',
           "# Tokens come from the macOS Keychain at the start, never from this file.",
           'theme = "auto"',
           "# Account connectors off: only this agent's identity counts.",
           "enable_connectors = false", ""]
    for name in ("ocellio", "moshi"):
        if name not in servers:
            continue
        out += ["[[mcp_servers]]", f'name = "{name}"', 'transport = "streamable-http"', f'url = "{servers[name]}"',
                "[mcp_servers.auth]", 'type = "static"', f'api_key_env = "{TOKEN_ENV[name]}"',
                'api_key_header = "Authorization"', 'api_key_format = "Bearer {token}"', ""]
    out += ["# Reading, reports and the mesh run without asking; everything else asks,",
            "# and without a screen to ask on Vibe refuses. No --auto-approve. Names: {server}_{tool}."]
    for name in ("ocellio", "moshi"):
        if name in servers:
            out += [f'[tools.{name}_{t}]\npermission = "always"' for t in ALWAYS[name]]
    for name in ("ocellio", "moshi"):
        if name in servers:
            out += [f'[tools.{name}_{t}]\npermission = "ask"' for t in ASK[name]]
    return "\n".join(out) + "\n"


class Vibe(Harness):
    name = "vibe"
    binary = "vibe"
    resume_flag = ("--resume",)

    def home(self, ctx: Ctx, agent: Agent) -> Path:
        return Path(agent.home) if agent.home else ctx.paths.agent_home(agent.name) / "vibe"

    def setup(self, ctx: Ctx, agent: Agent) -> List[str]:
        home = self.home(ctx, agent)
        home.mkdir(parents=True, exist_ok=True)
        home.chmod(0o700)
        changed = []
        if ctx.backup.write_text(home / "config.toml", vibe_config(ctx.config.servers), mode=0o600):
            changed.append(str(home / "config.toml"))
        if ctx.backup.write_text(home / "AGENTS.md", self.presence(ctx, agent)):
            changed.append(str(home / "AGENTS.md"))
        return changed

    def command(self, ctx, agent, extra):
        argv, env = super().command(ctx, agent, extra)
        env["VIBE_HOME"] = str(self.home(ctx, agent))
        return argv, env

    def session_home(self, ctx: Ctx, agent: Agent) -> Optional[Path]:
        return self.home(ctx, agent)

    def findings(self, ctx: Ctx, agent: Agent) -> List[str]:
        config = self.home(ctx, agent) / "config.toml"
        if not config.is_file():
            return [f"{config} is missing (moshi-cockpit install)"]
        if config.read_text() != vibe_config(ctx.config.servers):
            return [f"Vibe profile {config} differs from what moshi-cockpit writes (servers, approvals); "
                    "moshi-cockpit install restores it"]
        return []
