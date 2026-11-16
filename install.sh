#!/bin/sh
# moshi-cockpit installer entry (macOS arm64). Checks the prerequisites and hands over to `moshi-cockpit install`.
# Every argument goes through, e.g.: ./install.sh --moshi https://moshi.example.test/mcp
set -eu
root=$(cd "$(dirname "$0")" && pwd)

fail() { echo "moshi-cockpit: $*" >&2; exit 1; }

[ "$(uname -s)" = Darwin ] || fail "only macOS is supported"
[ "$(uname -m)" = arm64 ] || fail "only Apple silicon (arm64) is supported"

py=${MOSHI_COCKPIT_PYTHON:-python3}
command -v "$py" >/dev/null 2>&1 || fail "python3 is missing (xcode-select --install provides /usr/bin/python3)"
"$py" -I -c 'import sys; sys.exit(0 if sys.version_info >= (3, 9) else 1)' \
  || fail "python3 >= 3.9 needed, found: $("$py" --version 2>&1)"

command -v security >/dev/null 2>&1 || fail "security is missing"

exec "$root/bin/moshi-cockpit" install "$@"
