#!/bin/sh
# Every check before a push, failing fast: leak guard (files and commits), Python suite on the system
# Python 3.9 and on 3.12 if present, the mods' JS tests, shellcheck. There is no CI: run this, and put its
# last lines into the commit or PR description. The leak guard needs the private word list (see README).
set -eu
cd "$(dirname "$0")/.."

step() { printf '\n== %s\n' "$*"; }

step "leak guard: files"
python3 tools/leak_check.py

if git rev-parse --verify -q origin/main >/dev/null; then
  range="origin/main..HEAD"
else
  range="HEAD"  # not pushed yet: the whole history
fi
if git rev-parse --verify -q HEAD >/dev/null; then
  step "leak guard: commits $range"
  python3 tools/leak_check.py --commits "$range"
fi

step "python: /usr/bin/python3"
/usr/bin/python3 -m unittest discover -s tests -t . -v

if command -v python3.12 >/dev/null 2>&1; then
  step "python: python3.12"
  python3.12 -m unittest discover -s tests -t . -v
fi

step "node: mods"
node --test tests/js/*.test.mjs

step "shellcheck"
shellcheck install.sh bin/* tools/check.sh tools/hooks/*

step "all checks passed"
