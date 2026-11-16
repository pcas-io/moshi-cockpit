"""No auto-approve path for the agents (a security guardrail of the person model).

The person model only works if "ask" really asks. So: no grant that answers an ASK tool for the
person, no skip-permissions/yolo/always-approve setting that the mesh agents inherit, and the
panel starts the account agents (claude, grok) in a mode that asks.
"""

from __future__ import annotations

import fnmatch
import json
import re
from typing import Dict, List, Sequence

from cockpit.paths import Paths
from cockpit.person import ALWAYS, ASK
from cockpit.text import clean

# Settings that switch asking off, wherever they turn up in an agent's settings (truthy = finding).
_SKIP_KEY = re.compile(r"(skip.?permission|dangerously|yolo|auto.?approve|always.?approve|bypass)", re.I)
# Modes that answer for the person. Grok: permission_mode; Claude: --permission-mode.
GROK_UNSAFE_MODES = {"always-approve", "bypassPermissions", "dontAsk", "auto"}
PANEL_ARGS = {"grok": ["--permission-mode", "default"], "claude": ["--permission-mode", "manual"]}


def agy_rule_grants_ask(rule: str) -> bool:
    """True for an agy allow rule that would answer any ASK tool. Fail-closed: the rule's server and
    tool parts are matched as globs (case-insensitive) against every ASK server/tool, so wildcards in
    either part (`mcp(ocel*/update_entity)`, `mcp(*ellio/*)`, `mcp(ocellio/u*_entity)`) count too."""
    match = re.fullmatch(r"\s*mcp\s*(?:\((.*)\))?\s*", str(rule), re.I)
    if not match:
        return False
    target = (match.group(1) or "").strip().lower() or "*"
    server_glob, sep, tool_glob = target.partition("/")
    tool_glob = tool_glob if sep else "*"
    return any(fnmatch.fnmatchcase(server, server_glob or "*") and fnmatch.fnmatchcase(tool, tool_glob or "*")
               for server, tools in ASK.items() for tool in tools)


# Keys whose value says how approvals work; "auto"/"always" there means nobody asks.
_MODE_KEY = re.compile(r"(approv|permission|mode|policy)", re.I)


def _switches_off(value: str, key: str) -> bool:
    """A string value that turns asking off: anywhere when it reads like it ("yolo", "bypass…"),
    and "auto"/"always" under a key about approvals, permissions, modes or policies."""
    return bool(_SKIP_KEY.search(value)) or (bool(_MODE_KEY.search(key)) and value.strip().lower() in ("auto", "always"))


# A settings name is plain words (camelCase, snake_case, kebab-case), no digits. Anything else in a path
# (e.g. a key that is itself a token, like "bt_bypass_456789") is masked: a key can be a secret too.
# No optional separator inside the repeat: "[A-Za-z]+(?:[_-]?[A-Za-z]+)*" backtracks exponentially on
# long letter runs ending in a digit (found in review: 26 letters + "1" took 2.6 s). camelCase is
# already covered by [A-Za-z]+, so a mandatory separator is equivalent and linear.
_PLAIN_KEY = re.compile(r"[A-Za-z]+(?:[_-][A-Za-z]+)*")
MASKED_KEY = "<verborgen>"


def _shown_key(name: object) -> str:
    text = str(name)
    return text if len(text) <= 48 and _PLAIN_KEY.fullmatch(text) else MASKED_KEY


def _truthy_skip_keys(data: object, path: str = "", key: str = "") -> List[str]:
    """Paths of settings that switch asking off: by key (`dangerouslySkipPermissions: true`) or by value
    (`approvalMode: "yolo"`), also bare strings in lists (`approvalModes: ["yolo"]`). `key` is the
    nearest dict key, so list items are judged by the key they belong to. Only paths are returned,
    never values: a matching value can be a secret (`apiKey: "secret_yolo_123"`)."""
    found = []
    if isinstance(data, dict):
        for name, value in data.items():
            shown = _shown_key(name)
            here = f"{path}.{shown}" if path else shown
            if _SKIP_KEY.search(str(name)) and value not in (False, None, "", 0, [], {}):
                found.append(here)
            elif isinstance(value, str) and _switches_off(value, str(name)):
                found.append(here)  # the path only, never the value: it could be a secret
            found.extend(_truthy_skip_keys(value, here, str(name)))
    elif isinstance(data, list):
        for i, value in enumerate(data):
            here = f"{path}[{i}]"
            if isinstance(value, str) and _switches_off(value, key):
                found.append(here)  # the path only, never the value
            found.extend(_truthy_skip_keys(value, here, key))
    return found


def agy_findings(paths: Paths) -> List[str]:
    try:
        settings = json.loads(paths.agy_settings.read_text())
    except (OSError, ValueError):
        return []
    findings = []
    rules = (settings.get("permissions") or {}).get("allow") or [] if isinstance(settings, dict) else []
    granting = sorted(r for r in rules if agy_rule_grants_ask(r))
    if granting:
        findings.append(f"agy: Freigaben beantworten Nachfrage-Tools für dich: {granting}")
    skips = _truthy_skip_keys(settings)
    if skips:
        shown = ", ".join(clean(path, "?", limit=120) for path in skips)
        findings.append(f"agy: Einstellungen schalten vermutlich das Nachfragen ab (Werte nicht angezeigt): {shown}")
    return findings


def grok_findings(paths: Paths) -> List[str]:
    """A known gap: the local Grok CLI's own config can approve every tool. A start through moshi-cockpit overrides it, a direct
    `grok` start does not."""
    try:
        text = paths.grok_config.read_text()
    except OSError:
        return []
    findings = []
    mode = re.search(r"""^\s*permission_mode\s*=\s*["']([^"']*)["']""", text, re.M)
    if mode and mode.group(1) in GROK_UNSAFE_MODES:
        findings.append(f'grok: permission_mode = "{mode.group(1)}" gibt alle Tools ohne Nachfrage frei '
                        "(der Start über moshi-cockpit überschreibt das, ein direkter grok-Start nicht)")
    if re.search(r"^\s*yolo\s*=\s*true\b", text, re.M):
        findings.append("grok: yolo = true gibt alle Tools ohne Nachfrage frei")
    return findings


def codex_args(presence: str, servers: Dict[str, str], token_env: Dict[str, str]) -> List[str]:
    """Codex as mesh agent: -c overrides only, ~/.codex stays untouched. Only configured servers are
    wired in. Every tool's approval is set explicitly, so nothing inherited from ~/.codex/config.toml
    can answer for the person, and the approvals reviewer is the person, not a model (`auto_review`)."""
    args = ["-c", 'approvals_reviewer="user"']
    for server in ("ocellio", "moshi"):
        if server not in servers:
            continue
        args += ["-c", f"mcp_servers.{server}.url={json.dumps(servers[server])}",
                 "-c", f'mcp_servers.{server}.bearer_token_env_var="{token_env[server]}"',
                 "-c", f"mcp_servers.{server}.enabled_tools={json.dumps(ALWAYS[server] + ASK[server])}",
                 "-c", f'mcp_servers.{server}.default_tools_approval_mode="prompt"']
        args += _modes(server, ALWAYS[server], "approve") + _modes(server, ASK[server], "prompt")
    # A JSON string is a valid TOML basic string (\n, \", \uXXXX), so line breaks survive.
    args += ["-c", f"developer_instructions={json.dumps(presence, ensure_ascii=False)}"]
    return args


def _modes(server: str, tools: Sequence[str], mode: str) -> List[str]:
    out: List[str] = []
    for tool in tools:
        out += ["-c", f'mcp_servers.{server}.tools.{tool}.approval_mode="{mode}"']
    return out


def tool_lists() -> Dict[str, List[str]]:
    """For docs and the panel: what runs unasked and what asks."""
    return {"always": [f"{s}/{t}" for s in ALWAYS for t in ALWAYS[s]],
            "ask": [f"{s}/{t}" for s in ASK for t in ASK[s]]}
