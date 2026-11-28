"""grok (xAI's CLI): each agent has its own moshi identity, like claude. grok reads one global config
(`~/.grok/config.toml`), where the login lives too, so the moshi server is written there once, with an
Authorization header that is nothing but a variable: grok expands `${VAR:-}` in `[mcp_servers.*]` headers when
it loads the config. `moshi-cockpit run` sets that variable to `Bearer <the agent's own token>`; a plain `grok`
start has none, sends an empty header and gets no moshi identity. The person's grok login stays as it is.

Two details are deliberate:
- The variable is grok's own (GROK_AUTH_ENV), not the MESH_MOSHI_TOKEN every agent carries. A grok that another
  agent starts from its shell inherits that agent's environment; with the shared name it would speak in moshi as
  that agent. A grok started by a grok agent still inherits the grok variable and acts as that same agent.
- The whole header comes from the variable, with an empty default. A `Bearer ${TOKEN}` header would send
  `Bearer ` or the literal placeholder without a token, and moshi counts every wrong Bearer of an address
  towards its sign-in throttle, the dashboard's included. An empty header is no credential and counts nothing.

The block is marked, written through the backup store (uninstall restores the file) and replaced as a whole.
A `[mcp_servers.moshi]` the person wrote outside the block is left alone and reported: a second table of the
same name would break the file.
"""

from __future__ import annotations

import json
import re
from typing import Dict, List, Sequence, Tuple

from cockpit.approvals import grok_findings
from cockpit.harnesses.base import TOKEN_ENV, Ctx, Harness
from cockpit.policy import MOSHI_ALWAYS
from cockpit.registry import Agent

GROK_AUTH_ENV = "MESH_GROK_MOSHI_AUTH"  # "Bearer <token>", set only by `moshi-cockpit run` for a grok agent
BEGIN = "# >>> moshi-cockpit: moshi for grok agents started with `moshi-cockpit run` (own token from the environment)"
END = "# <<< moshi-cockpit"
_BLOCK = re.compile(re.escape(BEGIN) + r".*?" + re.escape(END) + r"\n?", re.S)
_FOREIGN = re.compile(r'^\s*\[\s*mcp_servers\s*\.\s*"?moshi"?\s*[\].]', re.M)
FOREIGN_FINDING = ("~/.grok/config.toml already has its own [mcp_servers.moshi]; moshi-cockpit leaves it alone. "
                   "Remove it there, then run moshi-cockpit install")


def moshi_block(url: str) -> str:
    return "\n".join([BEGIN, "[mcp_servers.moshi]", f"url = {json.dumps(url)}",
                      'headers = { "Authorization" = "${' + GROK_AUTH_ENV + ':-}" }', END]) + "\n"


def without_block(text: str) -> str:
    return _BLOCK.sub("", text)


def with_block(text: str, url: str) -> str:
    """`text` with exactly one current block at its end. ValueError if the person has a moshi server of their
    own outside the block."""
    rest = without_block(text)
    if _FOREIGN.search(rest):
        raise ValueError(FOREIGN_FINDING)
    rest = rest.rstrip("\n")
    return (rest + "\n\n" if rest else "") + moshi_block(url)


class Grok(Harness):
    name = "grok"
    binary = "grok"
    mesh_servers = ("moshi",)  # ocellio stays as grok itself is set up (it reads the person's Claude config)
    clean_env = False
    resume_flag = ("--resume",)
    permission_args = ("--permission-mode", "default")

    def setup(self, ctx: Ctx, agent: Agent) -> List[str]:
        if "moshi" not in self.servers(ctx):
            return []
        path = ctx.paths.grok_config
        current = path.read_text() if path.is_file() else ""
        path.parent.mkdir(parents=True, exist_ok=True)
        if ctx.backup.write_text(path, with_block(current, ctx.config.servers["moshi"]), mode=0o600):
            return [str(path)]
        return []

    def command(self, ctx: Ctx, agent: Agent, extra: Sequence[str]) -> Tuple[List[str], Dict[str, str]]:
        """The header under grok's own name for its config; MESH_MOSHI_TOKEN stays for `moshi-cockpit ring`."""
        argv, env = super().command(ctx, agent, extra)
        if TOKEN_ENV["moshi"] in env:
            env[GROK_AUTH_ENV] = f"Bearer {env[TOKEN_ENV['moshi']]}"
        return argv, env

    def args(self, ctx: Ctx, agent: Agent) -> List[str]:
        allowed: List[str] = []
        if "moshi" in self.servers(ctx):
            for tool in MOSHI_ALWAYS:
                allowed += ["--allow", f"MCPTool(moshi__{tool})"]
        return [*allowed, "--rules", self.presence(ctx, agent)]

    def findings(self, ctx: Ctx, agent: Agent) -> List[str]:
        found = grok_findings(ctx.paths)
        if "moshi" not in self.servers(ctx):
            return found
        path = ctx.paths.grok_config
        text = path.read_text() if path.is_file() else ""
        if _FOREIGN.search(without_block(text)):
            return found + [FOREIGN_FINDING]
        if moshi_block(ctx.config.servers["moshi"]) not in text:
            return found + ["~/.grok/config.toml has no current moshi entry for grok agents; moshi-cockpit install "
                            "writes it"]
        return found
