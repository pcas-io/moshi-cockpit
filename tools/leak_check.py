#!/usr/bin/env python3
"""Leak guard: keeps internal ids, private email addresses and blocked words out of files and commits.

    python3 tools/leak_check.py                    scan every tracked or new (not ignored) file and its path
    python3 tools/leak_check.py --commits RANGE    scan author, committer and message of each commit in RANGE

A finding prints `path:line: <rule>` (line 0 is the path itself) and the exit code is 1. The matched text is
never printed; path parts that trip a rule are shown as `***`.

The blocked words are private and never part of the repo, not even as hashes (short words fall to a
dictionary). They are read from the file in $MOSHI_COCKPIT_BLOCKED_WORDS, by default
~/.config/moshi-cockpit-dev/blocked-words.txt: one word per line, `#` starts a comment. Without that list
the guard cannot judge anything and fails closed with exit code 2.
"""

from __future__ import annotations

import argparse
import os
import re
import subprocess
import sys
from pathlib import Path
from typing import Callable, Iterable, List, Mapping, Optional

WORDS_ENV = "MOSHI_COCKPIT_BLOCKED_WORDS"
DEFAULT_WORDS = Path(".config") / "moshi-cockpit-dev" / "blocked-words.txt"
NO_WORDS = f"leak_check: private word list not found (set {WORDS_ENV})"

ALLOWED_EMAIL_DOMAINS = frozenset({"example.test", "example.invalid", "users.noreply.github.com", "anthropic.com"})
COMMIT_FORMAT = "%an%n%ae%n%cn%n%ce%n%B"

# Knowledge-graph ids: the short form of the current id generation, full UUIDv7, and any standalone 8-hex
# token (letters and digits) on a line that talks about ids.
OCELLIO_ID = re.compile(
    r"\b06a[0-9a-f]{5}\b|\b[0-9a-f]{8}-[0-9a-f]{4}-7[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}\b")
SHORT_HEX = re.compile(r"\b(?=[0-9a-f]*[0-9])(?=[0-9a-f]*[a-f])[0-9a-f]{8}\b")
ID_WORD = re.compile(r"\b(?:ocellio|entity|refs?|closes)\b|\bid:", re.I)
EMAIL = re.compile(r"[a-z0-9._%+-]+@((?:[a-z0-9-]+\.)+[a-z]{2,})", re.I)
# A hash sign and a number right after a word: a repo name, `PR`, `issue` or `owner/repo`, with or without a
# space. Colours, `$#` and list numbers at the start of a line have no word directly in front of them.
ISSUE_REF = re.compile(r"(?:\b(?:pr|pull|issues?|ticket|bug)\s*|[a-z0-9_-]\s?)#\d+\b", re.I)
DECISION_CODE = re.compile(r"(?i:\b(?:residual risk|restrisiko)\s*\d+)|\bWP-\d+|\bADR\b")
TOKEN = re.compile(r"[a-z0-9][a-z0-9.-]*")
# Word breaks inside CamelCase: lower or digit before upper (`newWord`, `v2Word`), and the last capital of an
# acronym before a capitalised word (`WORDClient`).
CAMEL = re.compile(r"(?<=[a-z0-9])(?=[A-Z])|(?<=[A-Z])(?=[A-Z][a-z])")

Git = Callable[[List[str], Path], str]


def load_words(env: Mapping[str, str]) -> frozenset:
    """The private list, lower-cased; empty if the file is missing or holds no word."""
    home = Path(env.get("HOME") or str(Path.home()))
    path = Path(env.get(WORDS_ENV) or str(home / DEFAULT_WORDS))
    try:
        lines = path.read_text().splitlines()
    except OSError:
        return frozenset()
    return frozenset(w for w in (line.split("#", 1)[0].strip().lower() for line in lines) if w)


def candidates(token: str) -> Iterable[str]:
    """Every contiguous run of the token's atoms (split on `.` and `-`), joined with their own separators."""
    parts = re.split(r"([.-])", token.strip(".-"))
    atoms, seps = parts[0::2], parts[1::2]
    for start in range(len(atoms)):
        run = atoms[start]
        yield run
        for end in range(start + 1, len(atoms)):
            run += seps[end - 1] + atoms[end]
            yield run


def has_blocked_word(text: str, words: frozenset) -> bool:
    for variant in (text, CAMEL.sub(" ", text)):  # the second pass splits CamelCase compounds
        for token in TOKEN.findall(variant.lower()):
            if any(c in words for c in candidates(token)):
                return True
    return False


def scan_line(line: str, words: frozenset) -> List[str]:
    """Names of the rules this line breaks, in a fixed order."""
    rules = []
    lower = line.lower()
    if OCELLIO_ID.search(lower) or (ID_WORD.search(line) and SHORT_HEX.search(lower)):
        rules.append("ocellio-id")
    if any(domain.lower() not in ALLOWED_EMAIL_DOMAINS for domain in EMAIL.findall(line)):
        rules.append("email")
    if ISSUE_REF.search(line):
        rules.append("issue-ref")
    if DECISION_CODE.search(line):
        rules.append("decision-code")
    if has_blocked_word(line, words):
        rules.append("blocked-word")
    return rules


def run_git(args: List[str], cwd: Path) -> str:
    return subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True, check=True).stdout


def shown_path(path: str, words: frozenset) -> str:
    return "/".join("***" if scan_line(part, words) else part for part in path.split("/"))


def scan_files(root: Path, words: frozenset, git: Git) -> List[str]:
    findings = []
    listed = git(["ls-files", "-z", "--cached", "--others", "--exclude-standard"], root)
    for name in sorted(set(filter(None, listed.split("\0")))):
        path = root / name
        if not path.is_file():
            continue  # deleted but still in the index
        shown = shown_path(name, words)
        findings += [f"{shown}:0: {rule}" for rule in scan_line(name, words)]
        data = path.read_bytes()
        if b"\0" in data:
            continue  # binary
        for number, line in enumerate(data.decode("utf-8", "replace").splitlines(), 1):
            findings += [f"{shown}:{number}: {rule}" for rule in scan_line(line, words)]
    return findings


def scan_commits(root: Path, revisions: str, words: frozenset, git: Git) -> List[str]:
    findings = []
    for sha in git(["rev-list", revisions], root).split():
        text = git(["show", "-s", f"--format={COMMIT_FORMAT}", sha], root)
        for number, line in enumerate(text.splitlines(), 1):
            findings += [f"commit {sha[:12]}:{number}: {rule}" for rule in scan_line(line, words)]
    return findings


def main(argv: Optional[List[str]] = None, words: Optional[frozenset] = None, cwd: Optional[Path] = None,
         git: Git = run_git, env: Optional[Mapping[str, str]] = None) -> int:
    parser = argparse.ArgumentParser(description="Scan files or commits for internal data.")
    parser.add_argument("--commits", metavar="RANGE", help="scan commit metadata in RANGE instead of files")
    args = parser.parse_args(argv)
    if words is None:
        words = load_words(os.environ if env is None else env)
    if not words:
        print(NO_WORDS, file=sys.stderr)
        return 2
    root = Path(cwd) if cwd else Path(run_git(["rev-parse", "--show-toplevel"], Path.cwd()).strip())
    if args.commits:
        findings = scan_commits(root, args.commits, words, git)
    else:
        findings = scan_files(root, words, git)
    for finding in findings:
        print(finding)
    return 1 if findings else 0


if __name__ == "__main__":
    sys.exit(main())
