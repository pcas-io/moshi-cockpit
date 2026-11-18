#!/bin/sh
# moshi-cockpit installer for macOS on Apple silicon, and headless (without Herdr) for Linux on x86_64 and aarch64:
#   curl -fsSL https://github.com/pcas-io/moshi-cockpit/releases/latest/download/install.sh | sh
# Flags go after `sh -s --`: ... | sh -s -- --moshi https://moshi.example.test/mcp --ocellio https://ocellio.example.test/mcp/
#
# This file in the repo is a template. tools/build_release.py renders it for each release with the version
# and the SHA-256 of everything it downloads, so a release's install.sh installs exactly that release. Both
# downloads (the Python pinned for this platform and the app) are checked before anything of them is unpacked or
# run. Herdr comes from herdr.dev, never from here; a Linux machine needs no Herdr app: Herdr on the Mac drives it.
# Everything runs inside main(): sh reads the whole script from the pipe before it runs a line of it.
set -eu

VERSION='@VERSION@'
PAYLOAD_SHA256='@PAYLOAD_SHA256@'
RELEASE_URL='@RELEASE_URL@'
PYTHON_VERSION='@PYTHON_VERSION@'
PYTHON_URL_MACOS_ARM64='@PYTHON_URL_MACOS_ARM64@'
PYTHON_SHA256_MACOS_ARM64='@PYTHON_SHA256_MACOS_ARM64@'
PYTHON_URL_LINUX_X86_64='@PYTHON_URL_LINUX_X86_64@'
PYTHON_SHA256_LINUX_X86_64='@PYTHON_SHA256_LINUX_X86_64@'
PYTHON_URL_LINUX_AARCH64='@PYTHON_URL_LINUX_AARCH64@'
PYTHON_SHA256_LINUX_AARCH64='@PYTHON_SHA256_LINUX_AARCH64@'
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

current() {  # PYTHON SERVER: the address the settings hold now, printable characters only; empty on any doubt
  "$1" -I -c '
import json, sys
try:
    with open(sys.argv[1], encoding="utf-8") as handle:
        value = json.load(handle)["servers"][sys.argv[2]]
except Exception:
    value = ""
print("".join(ch for ch in str(value) if 32 < ord(ch) < 127)[:200])
' "$HOME/.config/moshi-cockpit/config.json" "$2" </dev/null 2>/dev/null || true
}

download() {  # URL FILE WHAT
  curl --proto "$CURL_PROTO" --proto-redir "$CURL_PROTO" --tlsv1.2 -fsSL --retry 2 -o "$2" "$1" \
    || fail "download failed ($3). Nothing was installed. Check the connection and run the line again"
}

sha256() {  # FILE: prints its SHA-256 (and the name)
  if [ -n "$has_sha256sum" ]; then sha256sum "$1"; else shasum -a 256 "$1"; fi
}

verify() {  # FILE SHA256 WHAT
  got=$(sha256 "$1" | cut -d ' ' -f 1)
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

  # The platform picks the pinned Python; only the Mac runs Herdr here, a Linux machine installs headless.
  case "$(uname -s) $(uname -m)" in
    "Darwin arm64")
      PYTHON_URL=$PYTHON_URL_MACOS_ARM64 PYTHON_SHA256=$PYTHON_SHA256_MACOS_ARM64 need_herdr=1 ;;
    "Linux x86_64")
      PYTHON_URL=$PYTHON_URL_LINUX_X86_64 PYTHON_SHA256=$PYTHON_SHA256_LINUX_X86_64 need_herdr="" ;;
    "Linux aarch64" | "Linux arm64")
      PYTHON_URL=$PYTHON_URL_LINUX_AARCH64 PYTHON_SHA256=$PYTHON_SHA256_LINUX_AARCH64 need_herdr="" ;;
    *) fail "Windows follows; this release supports macOS on Apple silicon and Linux on x86_64 and aarch64." ;;
  esac
  for tool in curl tar mktemp; do
    command -v "$tool" >/dev/null 2>&1 || fail "$tool is missing on this machine"
  done
  has_sha256sum=""
  if command -v sha256sum >/dev/null 2>&1; then
    has_sha256sum=1
  elif ! command -v shasum >/dev/null 2>&1; then
    fail "sha256sum (or shasum) is missing on this machine"
  fi
  [ -n "${HOME:-}" ] || fail "HOME is not set"
  [ -z "$need_herdr" ] || [ -d "$HERDR_APP" ] || fail "Install Herdr from https://herdr.dev first, then run this again."

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
  # On an update the prompt shows the address set now.
  if has_tty; then
    python="$runtime/python/bin/python3"
    if [ -z "$moshi_set" ]; then
      now=$(current "$python" moshi)
      if [ -n "$now" ]; then
        moshi=$(ask "moshi address [current: $now] (leave empty to keep it):")
      else
        moshi=$(ask "moshi address (leave empty to use Herdr without moshi):")
      fi
    fi
    if [ -z "$ocellio_set" ]; then
      now=$(current "$python" ocellio)
      if [ -n "$now" ]; then
        ocellio=$(ask "ocellio address [current: $now] (leave empty to keep it):")
      else
        ocellio=$(ask "ocellio address (leave empty to skip):")
      fi
    fi
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
