"""Google agy: reads ONE global MCP config (~/.gemini/config/mcp_config.json), so one agy agent per
machine. agy sends header values literally (no variable expansion, verified by hand), so its tokens sit
in that 0600 file (a known gap: readable by any process of the same user). A first task comes only via -i."""

from __future__ import annotations

import json
from typing import Dict, List, Sequence

from cockpit.approvals import agy_findings, agy_rule_grants_ask
from cockpit.defaults import load_json
from cockpit.harnesses.base import Ctx, Harness
from cockpit.policy import ALWAYS, ASK
from cockpit.registry import Agent


def agy_mcp_config(servers: Dict[str, str], tokens: Dict[str, str]) -> Dict:
    return {"mcpServers": {
        name: {"serverUrl": servers[name], "headers": {"Authorization": f"Bearer {tokens[name]}"},
               "enabledTools": ALWAYS[name] + ASK[name], "disabled": False}
        for name in ("ocellio", "moshi") if name in servers}}


def agy_allow_rules(servers: Sequence[str]) -> List[str]:
    """Only the ALWAYS tools; the ASK tools are visible (enabledTools) and agy asks for them."""
    return [f"mcp({s}/{t})" for s in ("ocellio", "moshi") if s in servers for t in ALWAYS[s]]


def redacted(config: Dict) -> Dict:
    """agy's config with every header value replaced: comparable, and never a token in a message."""
    copy = json.loads(json.dumps(config))
    servers = copy.get("mcpServers") if isinstance(copy.get("mcpServers"), dict) else {}
    for server in servers.values():
        headers = server.get("headers") if isinstance(server, dict) else None
        if isinstance(headers, dict):
            for key, value in headers.items():
                headers[key] = "Bearer <token>" if str(value).startswith("Bearer ") else "<redacted>"
    return copy


class Agy(Harness):
    name = "agy"
    binary = "agy"
    single_instance = True
    single_reason = "liest eine globale Config"
    resume_flag = ("--conversation",)

    def map_extra(self, extra: Sequence[str]) -> List[str]:
        # herdr appends a first task as a bare argument; agy only takes it via -i.
        if len(extra) == 1 and not str(extra[0]).startswith("-"):
            return ["-i", str(extra[0])]
        return list(extra)

    def setup(self, ctx: Ctx, agent: Agent) -> List[str]:
        servers = {s: ctx.config.servers[s] for s in self.servers(ctx)}
        changed = []
        config = json.dumps(agy_mcp_config(servers, self.tokens(ctx, agent)), indent=2) + "\n"
        if ctx.backup.write_text(ctx.paths.agy_mcp_config, config, mode=0o600):
            changed.append(str(ctx.paths.agy_mcp_config))
        settings = load_json(ctx.paths.agy_settings)
        permissions = settings.setdefault("permissions", {})
        kept = [r for r in permissions.get("allow", []) if not agy_rule_grants_ask(r)]
        permissions["allow"] = sorted(set(kept) | set(agy_allow_rules(list(servers))))
        if ctx.backup.write_text(ctx.paths.agy_settings, json.dumps(settings, indent=2) + "\n"):
            changed.append(str(ctx.paths.agy_settings))
        if ctx.backup.write_text(ctx.paths.agy_agents_md, self.presence(ctx, agent)):
            changed.append(str(ctx.paths.agy_agents_md))
        return changed

    def findings(self, ctx: Ctx, agent: Agent) -> List[str]:
        config = ctx.paths.agy_mcp_config
        if not config.is_file():
            # The settings check stands on its own: an inherited grant matters even before setup.
            return [f"{config} fehlt (moshi-cockpit install)"] + agy_findings(ctx.paths)
        findings = []
        if config.stat().st_mode & 0o077:
            findings.append(f"{config} ist nicht 0600 (enthält Tokens)")
        servers = {s: ctx.config.servers[s] for s in self.servers(ctx)}
        expected = redacted(agy_mcp_config(servers, {s: "x" for s in servers}))
        if redacted(load_json(config)) != expected:
            findings.append(f"agy: {config} weicht von dem ab, was moshi-cockpit schreibt (Server, URLs, Tools); "
                            "moshi-cockpit install stellt sie wieder her")
        return findings + agy_findings(ctx.paths)
