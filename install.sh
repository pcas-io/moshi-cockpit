#!/bin/sh
# moshi-cockpit Installer-Einstieg (macOS arm64). Prüft die Voraussetzungen und übergibt an `moshi-cockpit install`.
# Alle Argumente gehen durch, z. B.: ./install.sh --agents vibe,codex
set -eu
root=$(cd "$(dirname "$0")" && pwd)

fail() { echo "moshi-cockpit: $*" >&2; exit 1; }

[ "$(uname -s)" = Darwin ] || fail "nur macOS wird unterstützt (MVP)"
[ "$(uname -m)" = arm64 ] || fail "nur Apple Silicon (arm64) wird unterstützt (MVP)"

py=${MOSHI_COCKPIT_PYTHON:-python3}
command -v "$py" >/dev/null 2>&1 || fail "python3 fehlt (xcode-select --install liefert /usr/bin/python3)"
"$py" -I -c 'import sys; sys.exit(0 if sys.version_info >= (3, 9) else 1)' \
  || fail "python3 >= 3.9 nötig, gefunden: $("$py" --version 2>&1)"

for tool in security codesign ditto curl; do
  command -v "$tool" >/dev/null 2>&1 || fail "$tool fehlt"
done

exec "$root/bin/moshi-cockpit" install "$@"
