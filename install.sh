#!/bin/sh
# moshi-cockpit installer for macOS on Apple silicon:
#   curl -fsSL https://github.com/pcas-io/moshi-cockpit/releases/latest/download/install.sh | sh
# Flags go after `sh -s --`: ... | sh -s -- --moshi https://moshi.example.test/mcp --ocellio https://ocellio.example.test/mcp/
#
# This file in the repo is a template. tools/build_release.py renders it for each release with the version
# and the SHA-256 of everything it downloads, so a release's install.sh installs exactly that release. Both
# downloads are checked before anything of them is unpacked or run. Herdr comes from herdr.dev, never from here.
# Everything runs inside main(): sh reads the whole script from the pipe before it runs a line of it.
set -eu

VERSION='@VERSION@'
PAYLOAD_SHA256='@PAYLOAD_SHA256@'
RELEASE_URL='@RELEASE_URL@'
PYTHON_VERSION='@PYTHON_VERSION@'
PYTHON_URL='@PYTHON_URL@'
PYTHON_SHA256='@PYTHON_SHA256@'
HERDR_APP='@HERDR_APP@'
CURL_PROTO='@CURL_PROTO@'
ONE_LINE='curl -fsSL https://github.com/pcas-io/moshi-cockpit/releases/latest/download/install.sh | sh'

fail() { echo "moshi-cockpit: $*" >&2; exit 1; }
say() { echo "• $*"; }

# A terminal to ask on: stdin is the pipe from curl, so questions and answers go through /dev/tty.
has_tty() { (true </dev/tty) 2>/dev/null; }

ask() {
  printf '%s ' "$1" >/dev/tty
  answer=""
  read -r answer </dev/tty || answer=""
  printf '%s' "$answer"
}

download() {  # URL FILE WHAT
  curl --proto "$CURL_PROTO" --proto-redir "$CURL_PROTO" --tlsv1.2 -fsSL --retry 2 -o "$2" "$1" \
    || fail "download failed ($3). Nothing was installed. Check the connection and run the line again"
}

verify() {  # FILE SHA256 WHAT
  got=$(shasum -a 256 "$1" | cut -d ' ' -f 1)
  [ "$got" = "$2" ] \
    || fail "checksum mismatch ($3): the download is not what this release published. Nothing was installed"
}

main() {
  case $VERSION in
    @*) fail "this is the installer template. Install a release with: $ONE_LINE" ;;
  esac

  moshi="" moshi_set="" ocellio="" ocellio_set=""
  while [ $# -gt 0 ]; do
    case $1 in
      --moshi) [ $# -ge 2 ] || fail "--moshi needs an address"; moshi=$2; moshi_set=1; shift 2 ;;
      --ocellio) [ $# -ge 2 ] || fail "--ocellio needs an address"; ocellio=$2; ocellio_set=1; shift 2 ;;
      *) fail "unknown argument: $1 (known: --moshi URL, --ocellio URL)" ;;
    esac
  done

  [ "$(uname -s)" = Darwin ] && [ "$(uname -m)" = arm64 ] \
    || fail "Windows and Linux follow; this release supports macOS on Apple silicon."
  for tool in curl shasum tar mktemp; do
    command -v "$tool" >/dev/null 2>&1 || fail "$tool is missing on this Mac"
  done
  [ -n "${HOME:-}" ] || fail "HOME is not set"
  [ -d "$HERDR_APP" ] || fail "Install Herdr from https://herdr.dev first, then run this again."

  data="$HOME/.local/share/moshi-cockpit"
  runtime="$data/runtime"
  target="$data/app/$VERSION"
  tmp=$(mktemp -d)
  trap 'rm -rf "$tmp"' EXIT
  trap 'exit 1' INT TERM

  # The runtime stays when it is the one this release pins (its hash is written next to it after the check).
  need_python=1
  if [ -x "$runtime/python/bin/python3" ] && [ "$(cat "$runtime/.sha256" 2>/dev/null || true)" = "$PYTHON_SHA256" ]; then
    need_python=""
  fi
  if [ -n "$need_python" ]; then
    say "Downloading Python $PYTHON_VERSION"
    download "$PYTHON_URL" "$tmp/python.tar.gz" Python
  fi
  say "Downloading moshi-cockpit $VERSION"
  download "$RELEASE_URL/moshi-cockpit-$VERSION.tar.gz" "$tmp/payload.tar.gz" moshi-cockpit
  [ -z "$need_python" ] || verify "$tmp/python.tar.gz" "$PYTHON_SHA256" Python
  verify "$tmp/payload.tar.gz" "$PAYLOAD_SHA256" moshi-cockpit

  if [ -n "$need_python" ]; then
    mkdir "$tmp/runtime"
    tar -xzf "$tmp/python.tar.gz" -C "$tmp/runtime"
    [ -x "$tmp/runtime/python/bin/python3" ] || fail "the Python download has an unexpected layout"
    mkdir -p "$runtime"
    rm -rf "$runtime/python" "$runtime/.sha256"
    mv "$tmp/runtime/python" "$runtime/python"
    printf '%s\n' "$PYTHON_SHA256" > "$runtime/.sha256"
  fi
  "$runtime/python/bin/python3" -I -c 'import ssl' </dev/null || fail "the bundled Python does not start"

  mkdir "$tmp/app"
  tar -xzf "$tmp/payload.tar.gz" -C "$tmp/app"
  [ -x "$tmp/app/moshi-cockpit-$VERSION/bin/moshi-cockpit" ] \
    || fail "the moshi-cockpit download has an unexpected layout"
  mkdir -p "$data/app"
  rm -rf "$target"
  mv "$tmp/app/moshi-cockpit-$VERSION" "$target"

  # An empty answer leaves the setting as it is; an explicit empty flag removes it (as with moshi-cockpit install).
  if has_tty; then
    [ -n "$moshi_set" ] || moshi=$(ask "moshi address (leave empty to use Herdr without moshi):")
    [ -n "$ocellio_set" ] || ocellio=$(ask "ocellio address (leave empty to skip):")
  fi
  set --
  if [ -n "$moshi_set" ] || [ -n "$moshi" ]; then set -- "$@" --moshi "$moshi"; fi
  if [ -n "$ocellio_set" ] || [ -n "$ocellio" ]; then set -- "$@" --ocellio "$ocellio"; fi

  # install links ~/.local/bin/moshi-cockpit to this version (through its backups, so uninstall removes it).
  say "Setting up moshi-cockpit $VERSION"
  if has_tty; then
    "$target/bin/moshi-cockpit" install "$@" </dev/tty
  else
    "$target/bin/moshi-cockpit" install "$@" </dev/null
  fi

  case ":${PATH:-}:" in
    *":$HOME/.local/bin:"*) ;;
    *) say "Add ~/.local/bin to your PATH, then your shell finds moshi-cockpit" ;;
  esac
}

main "$@"
