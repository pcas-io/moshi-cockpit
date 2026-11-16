"""OpenAI Codex: wired per agent with -c overrides at start (~/.codex stays untouched)."""

from __future__ import annotations

from cockpit.approvals import codex_args
from cockpit.harnesses.base import TOKEN_ENV, Harness


class Codex(Harness):
    name = "codex"
    binary = "codex"
    resume_flag = ("resume",)

    def args(self, ctx, agent):
        servers = {s: ctx.config.servers[s] for s in self.servers(ctx)}
        return codex_args(self.presence(ctx, agent), servers, TOKEN_ENV)
