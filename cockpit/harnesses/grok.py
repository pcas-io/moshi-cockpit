"""grok (xAI's CLI): each agent has its own moshi identity, like claude. grok reads one global config
(`~/.grok/config.toml`), where the login lives too, so the moshi server is written there once, with a header
that names the token variable instead of a token: grok expands `${VAR}` in `[mcp_servers.*]` headers when it
loads the config. `moshi-cockpit run` puts the agent's own token into that variable; a plain `grok` start has
none and gets no moshi identity. The person's grok login (`grok login`) stays as it is.

The block is marked, written through the backup store (uninstall restores the file) and replaced as a whole.
A `[mcp_servers.moshi]` the person wrote outside the block is left alone and reported: a second table of the
same name would break the file.
"""

from __future__ import annotations

import json
import re
from typing import List

from cockpit.approvals import grok_findings
from cockpit.harnesses.base import TOKEN_ENV, Ctx, Harness
from cockpit.policy import MOSHI_ALWAYS
from cockpit.registry import Agent

BEGIN = "# >>> moshi-cockpit: moshi for grok agents started with `moshi-cockpit run` (own token from the environment)"
END = "# <<< moshi-cockpit"
_BLOCK = re.compile(re.escape(BEGIN) + r".*?" + re.escape(END) + r"\n?", re.S)
_FOREIGN = re.compile(r'^\s*\[\s*mcp_servers\s*\.\s*"?moshi"?\s*[\].]', re.M)
FOREIGN_FINDING = ("~/.grok/config.toml already has its own [mcp_servers.moshi]; moshi-cockpit leaves it alone. "
                   "Remove it there, then run moshi-cockpit install")


def moshi_block(url: str) -> str:
    return "\n".join([BEGIN, "[mcp_servers.moshi]", f"url = {json.dumps(url)}",
                      'headers = { "Authorization" = "Bearer ${' + TOKEN_ENV["moshi"] + '}" }', END]) + "\n"


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
