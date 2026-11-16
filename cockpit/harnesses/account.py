"""claude: each agent has its own moshi identity (one agent, one identity): its token is passed in the environment
only, the server entry refers to it by name. ocellio stays as the CLI itself is set up (the person's token,
person model). grok: no per-agent wiring yet. Both start in a mode that asks, whatever their own config says."""

from __future__ import annotations

import json
from typing import List

from cockpit.approvals import grok_findings
from cockpit.harnesses.base import TOKEN_ENV, Ctx, Harness
from cockpit.person import MOSHI_ALWAYS
from cockpit.registry import Agent

BEARER = "Bearer ${" + TOKEN_ENV["moshi"] + "}"  # a placeholder the CLI fills in: no token in argv or files


class Claude(Harness):
    name = "claude"
    binary = "claude"
    clean_env = False  # unchanged from the panel start so far
    mesh_servers = ("moshi",)
    resume_flag = ("--resume",)
    permission_args = ("--permission-mode", "manual")
    shell_start = True
    # The account's claude.ai moshi connector would give the agent a second identity, the account's, which
    # reads and acknowledges its mail. Only its tools are denied; the account's other
    # connectors stay. As --settings, not --disallowedTools: that option is variadic and would swallow the task.
    # Both spellings: whether the glob is case-sensitive is not documented.
    DENY = ("mcp__claude_ai_moshi*", "mcp__claude_ai_Moshi*")

    def args(self, ctx: Ctx, agent: Agent) -> List[str]:
        permissions = {"deny": list(self.DENY)}
        wiring: List[str] = []
        if "moshi" in self.servers(ctx):
            server = {"type": "http", "url": ctx.config.servers["moshi"], "headers": {"Authorization": BEARER}}
            wiring = ["--mcp-config", json.dumps({"mcpServers": {"moshi": server}})]  # variadic: an option follows
            permissions["allow"] = [f"mcp__moshi__{tool}" for tool in MOSHI_ALWAYS]
        return [*wiring, "--settings", json.dumps({"permissions": permissions}),
                "--append-system-prompt", self.presence(ctx, agent)]


class Grok(Harness):
    name = "grok"
    binary = "grok"
    native_mesh = False  # own tokens need a per-agent GROK_HOME (its config is global, the login lives there)
    mesh_servers = ()
    clean_env = False
    resume_flag = ("--resume",)
    permission_args = ("--permission-mode", "default")
    no_own_agents = "verbindet sich über dein Konto und kann keine eigene moshi-Identität bekommen. Starte ihn über „+“ → Grok"

    def findings(self, ctx: Ctx, agent: Agent) -> List[str]:
        return grok_findings(ctx.paths)
