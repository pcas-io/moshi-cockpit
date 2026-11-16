"""`moshi-cockpit sessions <agent>`: resumable sessions of an agent, for the session list.

Read straight from where each CLI keeps them (formats checked by hand), newest first, optionally only those
whose folder is the given one (the dialog lists this folder first, then all). Titles and first prompts
are the person's own text and may contain secrets: they are shown locally only, cleaned of control
characters and cut short; nothing here is logged or sent anywhere.

| Agent  | Store                                                        | Resume                       |
|--------|--------------------------------------------------------------|------------------------------|
| claude | ~/.claude/projects/<folder with / and . as ->/<id>.jsonl     | --resume <id> (in its folder)|
| codex  | ~/.codex/sessions/YYYY/MM/DD/rollout-*.jsonl (session_meta)  | resume <id>                  |
| grok   | ~/.grok/sessions/<urlencoded folder>/<id>/summary.json        | --resume <id>                |
| vibe   | <VIBE_HOME des Agents oder ~/.vibe>/logs/session/unified/<id>/meta.json | --resume <id> |
| agy    | ~/.gemini/antigravity-cli/cache/conversation_metadata.json   | --conversation <id>          |
"""

from __future__ import annotations

import json
import os
import re
from pathlib import Path
from typing import Callable, Dict, Iterator, List, Optional
from urllib.parse import unquote

from cockpit.paths import Paths
from cockpit.text import clean

AGENTS = ("claude", "codex", "grok", "vibe", "agy")
MAX_JOURNAL_LINES = 20  # Vibe's first turn is at the very start of its journal
MAX_JOURNAL_CHARS = 4_000_000  # per line: a longer one is cut, fails to parse and leaves the title open
RESUME = {"claude": ["--resume"], "codex": ["resume"], "grok": ["--resume"], "vibe": ["--resume"],
          "agy": ["--conversation"]}
_ID_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{7,63}")
MAX_TITLE = 80
SCAN_FACTOR = 4  # look at a few more files than shown, the newest by mtime
MAX_INDEX_LINES = 20000


# The first message earlier versions of the panel sent is not a useful title: show the task.
_PANEL_PREFIX = "Melde dich gemäß Präsenzregel in moshi an"
_PANEL_TASK = re.compile(r"Dann dein Auftrag(?: von [^:\n]{1,40})?:")


def _readable(title: object) -> str:
    text = str(title or "").strip()
    if text.startswith(_PANEL_PREFIX):
        match = _PANEL_TASK.search(text)
        return text[match.end():].strip() if match else "(angemeldet, ohne Auftrag)"
    return text


def _session(sid: object, title: object, updated: object, cwd: object) -> Optional[Dict]:
    sid = str(sid or "")
    if not _ID_RE.fullmatch(sid):
        return None
    return {"id": sid, "title": clean(_readable(title), "(ohne Titel)", limit=MAX_TITLE),
            "updated": clean(updated, ""), "cwd": str(cwd or "")}


def _json_lines(path: Path, limit: int) -> Iterator[Dict]:
    try:
        with path.open("r", encoding="utf-8", errors="replace") as handle:
            for i, line in enumerate(handle):
                if i >= limit:
                    return
                try:
                    data = json.loads(line)
                except ValueError:
                    continue
                if isinstance(data, dict):
                    yield data
    except OSError:
        return


def _tail_json_lines(path: Path, size: int = 256 * 1024) -> List[Dict]:
    try:
        with path.open("rb") as handle:
            handle.seek(max(0, path.stat().st_size - size))
            chunk = handle.read().decode("utf-8", errors="replace")
    except OSError:
        return []
    out = []
    for line in chunk.splitlines()[1:]:
        try:
            data = json.loads(line)
        except ValueError:
            continue
        if isinstance(data, dict):
            out.append(data)
    return out


def _text_of(message: object) -> str:
    if isinstance(message, dict):
        message = message.get("content")
    if isinstance(message, list):
        parts = [p.get("text", "") for p in message if isinstance(p, dict) and p.get("type") in ("text", "input_text")]
        return " ".join(parts)
    return str(message or "")


def _newest(files: List[Path], count: int) -> List[Path]:
    def mtime(p: Path) -> float:
        try:
            return p.stat().st_mtime
        except OSError:
            return 0.0
    return sorted(files, key=mtime, reverse=True)[:count]


def _iso_mtime(path: Path) -> str:
    from datetime import datetime, timezone
    try:
        return datetime.fromtimestamp(path.stat().st_mtime, timezone.utc).isoformat()
    except OSError:
        return ""


def claude_project_dir(folder: str) -> str:
    return re.sub(r"[^A-Za-z0-9]", "-", folder)


def read_claude(paths: Paths, folder: Optional[str], limit: int, home: Optional[Path] = None) -> List[Dict]:
    root = paths.home / ".claude" / "projects"
    dirs = [root / claude_project_dir(folder)] if folder else [d for d in root.glob("*") if d.is_dir()]
    files = [f for d in dirs for f in d.glob("*.jsonl")]
    out = []
    for f in _newest(files, limit * SCAN_FACTOR):
        cwd, first = "", ""
        for entry in _json_lines(f, 200):
            cwd = cwd or entry.get("cwd") or ""
            if not first and entry.get("type") == "user" and not entry.get("isMeta"):
                first = _text_of(entry.get("message"))
            if cwd and first:
                break
        title = ""
        for entry in reversed(_tail_json_lines(f)):
            title = entry.get("customTitle") or entry.get("aiTitle") or (
                entry.get("summary") if entry.get("type") == "summary" else "")
            if title:
                break
        item = _session(f.stem, title or first, _iso_mtime(f), cwd)
        if item:
            out.append(item)
    return out


def _codex_names(paths: Paths) -> Dict[str, str]:
    """Names given with /rename: Codex keeps them in session_index.jsonl, the last line for an id counts."""
    names: Dict[str, str] = {}
    for entry in _json_lines(paths.home / ".codex" / "session_index.jsonl", MAX_INDEX_LINES):
        if entry.get("id") and entry.get("thread_name"):
            names[str(entry["id"])] = str(entry["thread_name"])
    return names


def read_codex(paths: Paths, folder: Optional[str], limit: int, home: Optional[Path] = None) -> List[Dict]:
    files = list((paths.home / ".codex" / "sessions").glob("*/*/*/rollout-*.jsonl"))
    names = _codex_names(paths)
    out = []
    for f in _newest(files, limit * SCAN_FACTOR * (4 if folder else 1)):
        meta, first = {}, ""
        for entry in _json_lines(f, 300):
            payload = entry.get("payload") if isinstance(entry.get("payload"), dict) else {}
            if entry.get("type") == "session_meta":
                meta = payload
            elif not first and entry.get("type") == "event_msg" and payload.get("type") == "user_message":
                first = str(payload.get("message") or "")
            elif (not first and entry.get("type") == "response_item" and payload.get("type") == "message"
                  and payload.get("role") == "user"):
                text = _text_of(payload).strip()
                # skip what Codex injects as user messages (<environment_context>, the AGENTS.md block)
                first = "" if text.startswith(("<", "# AGENTS.md instructions")) else text
            if meta and first:
                break
        item = _session(meta.get("id"), names.get(str(meta.get("id"))) or first, _iso_mtime(f), meta.get("cwd"))
        if item:
            out.append(item)
    return out


def read_grok(paths: Paths, folder: Optional[str], limit: int, home: Optional[Path] = None) -> List[Dict]:
    root = paths.home / ".grok" / "sessions"
    out = []
    for summary in root.glob("*/*/summary.json"):
        cwd = unquote(summary.parent.parent.name)
        if folder and cwd != folder:
            continue
        try:
            data = json.loads(summary.read_text())
        except (OSError, ValueError):
            continue
        info = data.get("info") if isinstance(data.get("info"), dict) else {}
        # generated_title also holds a name given with /rename (title_is_manual), session_summary never does
        item = _session(info.get("id") or summary.parent.name,
                        data.get("generated_title") or data.get("session_summary") or info.get("session_summary"),
                        data.get("updated_at") or info.get("updated_at"), cwd)
        if item:
            out.append(item)
    return out


def read_vibe(paths: Paths, folder: Optional[str], limit: int, home: Optional[Path] = None) -> List[Dict]:
    """Vibe keeps sessions per profile: `home` is the agent's VIBE_HOME (one profile per agent); without one, the
    standard entry's. A session is only stored once it has a first message: an empty one never shows up here."""
    out = []
    for meta_file in ((home or paths.standard_vibe_home) / "logs" / "session" / "unified").glob("*/meta.json"):
        try:
            meta = json.loads(meta_file.read_text())
        except (OSError, ValueError):
            continue
        if meta.get("archived_at"):
            continue
        env = meta.get("environment") if isinstance(meta.get("environment"), dict) else {}
        item = _session(meta.get("session_id"), meta.get("title") or _vibe_first_prompt(meta_file.parent),
                        meta.get("bumped_at") or meta.get("end_time") or meta.get("start_time"),
                        env.get("working_directory"))
        if item:
            out.append(item)
    return out


def _vibe_first_prompt(session_dir: Path) -> str:
    """Vibe keeps messages in chunks/*.json; the first user turn (turn_index 0) serves as the title."""
    for chunk in sorted((session_dir / "chunks").glob("*.json"))[:8]:
        try:
            entries = json.loads(chunk.read_text())
        except (OSError, ValueError):
            continue
        for entry in entries if isinstance(entries, list) else []:
            message = entry.get("message") if isinstance(entry, dict) else None
            if isinstance(message, dict) and message.get("role") == "user" and entry.get("turn_index") == 0:
                return _text_of(message)
    # Vibe 3.1 keeps a new session's first turn in its journal instead.
    for journal in sorted((session_dir / "journal").glob("*.jsonl"))[:1]:
        try:
            with journal.open(encoding="utf-8", errors="replace") as lines:
                for _ in range(MAX_JOURNAL_LINES):
                    record = json.loads(lines.readline(MAX_JOURNAL_CHARS) or "{}")
                    payload = record.get("payload") if isinstance(record, dict) else None
                    params = payload.get("params") if isinstance(payload, dict) else None
                    entries = params.get("entries") if isinstance(params, dict) else None
                    for entry in entries if isinstance(entries, list) else []:
                        if isinstance(entry, dict) and entry.get("role") == "user":
                            return _text_of(entry)
        except (OSError, ValueError):
            continue
    return ""


def read_agy(paths: Paths, folder: Optional[str], limit: int, home: Optional[Path] = None) -> List[Dict]:
    store = paths.home / ".gemini" / "antigravity-cli" / "cache" / "conversation_metadata.json"
    try:
        conversations = json.loads(store.read_text()).get("conversations") or {}
    except (OSError, ValueError, AttributeError):
        return []
    out = []
    for cid, data in conversations.items():
        summary = (data or {}).get("summary") or {}
        uris = summary.get("WorkspaceURIs") or []
        cwd = unquote(str(uris[0])[len("file://"):]) if uris and str(uris[0]).startswith("file://") else ""
        item = _session(summary.get("ID") or cid, summary.get("Title") or summary.get("Preview"),
                        summary.get("UpdatedAt"), cwd)
        if item:
            out.append(item)
    return out


READERS: Dict[str, Callable[..., List[Dict]]] = {
    "claude": read_claude, "codex": read_codex, "grok": read_grok, "vibe": read_vibe, "agy": read_agy}


def list_sessions(harness: str, paths: Paths, folder: Optional[str] = None, limit: int = 8,
                  home: Optional[Path] = None) -> List[Dict]:
    """Sessions of a harness, newest first. `home` narrows to one agent where the harness keeps
    sessions per agent (Vibe); the other CLIs keep one store per person."""
    if harness not in READERS:
        raise ValueError(f"Unbekannte Harness '{harness}'. Erlaubt: {', '.join(AGENTS)}")
    folder = os.path.normpath(folder) if folder else None
    limit = max(1, min(limit, 30))
    found = READERS[harness](paths, folder, limit, home)
    if folder:
        found = [s for s in found if os.path.normpath(s["cwd"] or "/") == folder]
    found.sort(key=lambda s: s["updated"], reverse=True)
    for s in found[:limit]:
        s["resume_args"] = RESUME[harness] + [s["id"]]
    return found[:limit]


def for_agent(paths: Paths, agent_name: str, folder: Optional[str], limit: int, ctx_factory) -> List[Dict]:
    """Sessions for a registered agent: its harness's store, narrowed to the agent where possible."""
    from cockpit.harnesses import HARNESSES
    from cockpit.registry import find
    agent = find(paths, agent_name)
    harness = HARNESSES[agent.harness]
    return list_sessions(agent.harness, paths, folder, limit, harness.session_home(ctx_factory(), agent))


def as_json(agent: str, folder: Optional[str], sessions: List[Dict]) -> str:
    return json.dumps({"agent": agent, "folder": folder or "", "sessions": sessions}, ensure_ascii=False)
