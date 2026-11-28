"""A SessionStart hook for Claude Code: a session started without `moshi-cockpit run` says, once, that it has no
moshi identity of its own. It blocks nothing and prints nothing for a session that has one.

The hook lives in `~/.claude/settings.json`, a file the person and other tools keep changing. So it is not
written through the backup store, whose uninstall would put back the whole file as it was: install adds one
entry, uninstall removes exactly that entry, and everything else in the file stays as it is.
"""

from __future__ import annotations

import json
import os
import shlex
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional

from cockpit.harnesses.base import TOKEN_ENV
from cockpit.paths import Paths

HOOK_ARGS = ("hook", "session-start")
EVENT = "SessionStart"
WARNING = ("This Claude session was started without moshi-cockpit: it has no moshi identity of its own. "
           "Start agents from the + menu or with moshi-cockpit run <name>.")
# Informs, does not forbid: whether such sessions may still use a shared account connector is the person's call
# (they switch that connector off themselves); `ring` refuses them anyway, for lack of a token of their own.
CONTEXT = ("This session has no moshi identity of its own (it was not started with moshi-cockpit run). Anything it "
           "sends through an account connector that other sessions share appears under that shared name, and "
           "`moshi-cockpit ring` refuses it. For work with other agents, prefer an agent started with "
           "moshi-cockpit run. Reading `moshi-cockpit roster` is fine.")


def settings_file(paths: Paths) -> Path:
    return paths.home / ".claude" / "settings.json"


def hook_command(paths: Paths) -> str:
    return " ".join(shlex.quote(part) for part in [str(paths.local_bin / "moshi-cockpit"), *HOOK_ARGS])


def _ours(entry: Any, command: str) -> bool:
    hooks = entry.get("hooks") if isinstance(entry, dict) else None
    return isinstance(hooks, list) and any(isinstance(h, dict) and h.get("command") == command for h in hooks)


def _load(path: Path) -> Optional[Dict]:
    try:
        data = json.loads(path.read_text()) if path.is_file() else {}
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def _write(path: Path, data: Dict) -> None:
    """Claude Code's own layout (two spaces, a final newline), atomically, keeping the file's mode."""
    mode = path.stat().st_mode & 0o777 if path.exists() else 0o600
    tmp = path.with_name(path.name + ".tmp-moshi-cockpit")
    fd = os.open(str(tmp), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, mode)
    with os.fdopen(fd, "w") as handle:
        handle.write(json.dumps(data, indent=2, ensure_ascii=False) + "\n")
    os.chmod(tmp, mode)
    os.replace(tmp, path)


def ensure(paths: Paths) -> str:
    if not (paths.home / ".claude").is_dir():
        return "Claude Code hook: Claude Code is not set up here"
    path = settings_file(paths)
    data = _load(path)
    if data is None:
        return "Claude Code hook: skipped, ~/.claude/settings.json is not valid JSON (not overwritten)"
    hooks = data.setdefault("hooks", {})
    entries = hooks.setdefault(EVENT, []) if isinstance(hooks, dict) else None
    if not isinstance(entries, list):
        return "Claude Code hook: skipped, the hooks in ~/.claude/settings.json have an unexpected shape"
    command = hook_command(paths)
    if any(_ours(entry, command) for entry in entries):
        return "Claude Code hook: unchanged"
    entries.append({"hooks": [{"type": "command", "command": command}]})
    _write(path, data)
    return "Claude Code hook: warns in sessions started without moshi-cockpit"


def remove(paths: Paths) -> str:
    path = settings_file(paths)
    data = _load(path) if path.is_file() else None
    hooks = data.get("hooks") if data else None
    entries = hooks.get(EVENT) if isinstance(hooks, dict) else None
    if not isinstance(entries, list):
        return "Claude Code hook: nothing to remove"
    command = hook_command(paths)
    kept: List[Any] = [entry for entry in entries if not _ours(entry, command)]
    if len(kept) == len(entries):
        return "Claude Code hook: nothing to remove"
    if kept:
        hooks[EVENT] = kept
    else:
        del hooks[EVENT]
    _write(path, data)
    return "Claude Code hook: removed from ~/.claude/settings.json"


def session_start(environ: Mapping[str, str], moshi_set_up: bool) -> str:
    """What the hook prints: nothing for a session with its own identity or without moshi."""
    if not moshi_set_up or environ.get(TOKEN_ENV["moshi"]):
        return ""
    return json.dumps({"systemMessage": WARNING,
                       "hookSpecificOutput": {"hookEventName": EVENT, "additionalContext": CONTEXT}})
