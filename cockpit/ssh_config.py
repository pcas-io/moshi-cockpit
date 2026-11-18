"""Host aliases from ~/.ssh/config, read-only.

Follows `Include` (absolute, `~/`, or relative to ~/.ssh; globs sorted; depth limit 16; each file once).
Skips wildcard and negated patterns and everything inside a `Match` block. Missing or unreadable files
are skipped. Files are only ever opened for reading.
"""

from __future__ import annotations

import glob
import os
import re
import shlex
from pathlib import Path
from typing import List, Set, Tuple

MAX_DEPTH = 16
_KEYWORD = re.compile(r"^([A-Za-z]+)(?:\s*=\s*|\s+)(.*)$")


def _split(line: str) -> Tuple[str, List[str]]:
    """Keyword (lower case) and values of one config line; ("", []) for comments and empty lines."""
    line = line.strip()
    if not line or line.startswith("#"):
        return "", []
    match = _KEYWORD.match(line)
    if not match:
        return "", []
    keyword = match.group(1) + " " + match.group(2)
    try:
        parts = shlex.split(keyword, comments=True)
    except ValueError:
        parts = keyword.split()
    if not parts:
        return "", []
    return parts[0].lower(), parts[1:]


def _expand(pattern: str, home: Path) -> List[Path]:
    if pattern.startswith("~/"):
        pattern = str(home / pattern[2:])
    elif not os.path.isabs(pattern):
        pattern = str(home / ".ssh" / pattern)
    return [Path(p) for p in sorted(glob.glob(pattern))]


def _walk(path: Path, home: Path, depth: int, seen: Set[str], hosts: List[str]) -> None:
    if depth > MAX_DEPTH:
        return
    try:
        key = os.path.realpath(path)
        if key in seen or not os.path.isfile(path):
            return
        seen.add(key)
        with open(path, "r", encoding="utf-8", errors="replace") as handle:
            lines = handle.read().splitlines()
    except OSError:
        return
    in_match = False
    for line in lines:
        keyword, values = _split(line)
        if keyword == "host":
            in_match = False
            for name in values:
                if name and name[0] != "!" and "*" not in name and "?" not in name and name not in hosts:
                    hosts.append(name)
        elif keyword == "match":
            in_match = True
        elif keyword == "include" and not in_match:
            for pattern in values:
                for target in _expand(pattern, home):
                    _walk(target, home, depth + 1, seen, hosts)


def read_hosts(home: Path) -> List[str]:
    """Host aliases of `home/.ssh/config` in file order, unique."""
    hosts: List[str] = []
    _walk(Path(home) / ".ssh" / "config", Path(home), 0, set(), hosts)
    return hosts
