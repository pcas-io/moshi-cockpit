"""A SessionStart hook for Claude Code: a session started without `moshi-cockpit run` says, once at its start,
that it has no moshi identity of its own. It blocks nothing and prints nothing for a session that has one.

The hook lives in `~/.claude/settings.json`, a file the person and other tools keep changing, sometimes as a
link into a dotfiles repository. So it is not written through the backup store, whose uninstall would put back
the whole file as it was:
- install adds one entry, uninstall removes exactly our hook command and nothing else;
- the file behind a link is changed, the link stays;
- UTF-8 both ways, the file's own indentation and final newline are kept, its mode too;
- a file that changed between reading and writing is left alone (one note, try again).
"""

from __future__ import annotations

import json
import os
import re
import shlex
import tempfile
from pathlib import Path
from typing import Any, Dict, Mapping, Optional, Tuple, Union

from cockpit.harnesses.base import TOKEN_ENV
from cockpit.paths import Paths

HOOK_ARGS = ("hook", "session-start")
EVENT = "SessionStart"
MATCHER = "startup"  # not on resume, clear or compact: once per session
WARNING = ("This Claude session was started without moshi-cockpit: it has no moshi identity of its own. "
           "Start agents from the + menu or with moshi-cockpit run <name>.")
# Informs, does not forbid: whether such sessions may still use a shared account connector is the person's call
# (they switch that connector off themselves); `ring` refuses them anyway, for lack of a token of their own.
CONTEXT = ("This session has no moshi identity of its own (it was not started with moshi-cockpit run). Anything it "
           "sends through an account connector that other sessions share appears under that shared name, and "
           "`moshi-cockpit ring` refuses it. For work with other agents, prefer an agent started with "
           "moshi-cockpit run. Reading `moshi-cockpit roster` is fine.")
PREFIX = "Claude Code hook: "


def settings_file(paths: Paths) -> Path:
    return paths.home / ".claude" / "settings.json"


def hook_command(paths: Paths) -> str:
    return " ".join(shlex.quote(part) for part in [str(paths.local_bin / "moshi-cockpit"), *HOOK_ARGS])


class _Changed(Exception):
    """The file changed between reading and writing."""


Stamp = Optional[Tuple[int, int]]


def _stamp(path: Path) -> Stamp:
    try:
        info = path.stat()
    except FileNotFoundError:
        return None
    return info.st_mtime_ns, info.st_size


def _read(path: Path) -> Tuple[Optional[Dict], str, Stamp]:
    """(data or None when it is not a JSON object, the text, the stamp it was read at)."""
    stamp = _stamp(path)
    text = path.read_text(encoding="utf-8") if stamp else ""
    try:
        data = json.loads(text) if text.strip() else {}
    except ValueError:
        return None, text, stamp
    return (data if isinstance(data, dict) else None), text, stamp


def _indent(text: str) -> Union[int, str, None]:
    found = re.search(r"\n([ \t]+)\S", text)
    if found:
        return found.group(1)
    return 2 if not text.strip() else None  # a new file gets Claude Code's layout, a one-line file stays one line


def _write(path: Path, data: Dict, text: str, stamp: Stamp) -> None:
    out = json.dumps(data, indent=_indent(text), ensure_ascii=False) + ("\n" if text.endswith("\n") or not text else "")
    mode = path.stat().st_mode & 0o777 if stamp else 0o600
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=f".{path.name}.", suffix=".moshi-cockpit")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(out)
        os.chmod(tmp, mode)
        if _stamp(path) != stamp:
            raise _Changed()
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


def _target(paths: Paths) -> Path:
    """The file to change: the one a link points to, so a dotfiles link stays a link."""
    return Path(os.path.realpath(settings_file(paths)))


def _is_ours(hook: Any, command: str) -> bool:
    return isinstance(hook, dict) and hook.get("command") == command


def _has_ours(entry: Any, command: str) -> bool:
    hooks = entry.get("hooks") if isinstance(entry, dict) else None
    return isinstance(hooks, list) and any(_is_ours(h, command) for h in hooks)


def ensure(paths: Paths) -> str:
    if not (paths.home / ".claude").is_dir():
        return PREFIX + "Claude Code is not set up here"
    path = _target(paths)
    try:
        data, text, stamp = _read(path)
        if data is None:
            return PREFIX + "skipped, ~/.claude/settings.json is not a valid JSON object (not overwritten)"
        hooks = data.setdefault("hooks", {})
        entries = hooks.setdefault(EVENT, []) if isinstance(hooks, dict) else None
        if not isinstance(entries, list):
            return PREFIX + "skipped, the hooks in ~/.claude/settings.json have an unexpected shape"
        command = hook_command(paths)
        if any(_has_ours(entry, command) for entry in entries):
            return PREFIX + "unchanged"
        entries.append({"matcher": MATCHER, "hooks": [{"type": "command", "command": command}]})
        _write(path, data, text, stamp)
    except _Changed:
        return PREFIX + "skipped, ~/.claude/settings.json changed while it was read; run install again"
    except (OSError, UnicodeError) as exc:
        return PREFIX + f"skipped, {exc.__class__.__name__} on ~/.claude/settings.json"
    return PREFIX + "warns at the start of sessions without moshi-cockpit"


def remove(paths: Paths) -> str:
    path = _target(paths)
    try:
        data, text, stamp = _read(path)
        hooks = data.get("hooks") if data else None
        entries = hooks.get(EVENT) if isinstance(hooks, dict) else None
        command = hook_command(paths)
        if not isinstance(entries, list) or not any(_has_ours(entry, command) for entry in entries):
            return PREFIX + "nothing to remove"
        kept = []
        for entry in entries:
            if _has_ours(entry, command):
                rest = [h for h in entry["hooks"] if not _is_ours(h, command)]
                if not rest:
                    continue  # the entry held only our hook
                entry = {**entry, "hooks": rest}
            kept.append(entry)
        if kept:
            hooks[EVENT] = kept
        else:
            del hooks[EVENT]
        if not hooks:
            del data["hooks"]
        _write(path, data, text, stamp)
    except _Changed:
        return PREFIX + "not removed, ~/.claude/settings.json changed while it was read; run uninstall again"
    except (OSError, UnicodeError) as exc:
        return PREFIX + f"not removed, {exc.__class__.__name__} on ~/.claude/settings.json"
    return PREFIX + "removed from ~/.claude/settings.json"


def session_start(environ: Mapping[str, str], moshi_set_up: bool) -> str:
    """What the hook prints: nothing for a session with its own identity or without moshi."""
    if not moshi_set_up or environ.get(TOKEN_ENV["moshi"]):
        return ""
    return json.dumps({"systemMessage": WARNING,
                       "hookSpecificOutput": {"hookEventName": EVENT, "additionalContext": CONTEXT}})
