"""Which agent CLIs (harnesses) a machine has, and whether each is signed in, without reading a secret.

The catalog is final: claude, codex, vibe, agy, grok, opencode. opencode has an installer but no moshi
adapter (no entry in `cockpit.harnesses.HARNESSES`).

The probe is one static sh script (`probe_script`): no input is ever interpolated into it. A sign-in is
judged only by an exit code or a file's existence; the vendor command's output goes to /dev/null, no file
content and no Keychain secret is read. Where a vendor documents no such check, the state is unknown and
shown as "Installed, not signed in".
"""

from __future__ import annotations

import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Dict, List, Optional, Tuple

from cockpit.paths import Paths

KNOWN: Tuple[str, ...] = ("claude", "codex", "vibe", "agy", "grok", "opencode")
BINARIES: Dict[str, str] = {name: name for name in KNOWN}
LABELS: Dict[str, str] = {"claude": "Claude Code", "codex": "Codex", "vibe": "Vibe", "agy": "Antigravity",
                          "grok": "Grok", "opencode": "OpenCode"}

# ~/.local/bin and ~/bin, plus the vendors' default install dirs (checked 2026-11-18):
# claude, agy, codex and vibe (uv tool) install into ~/.local/bin; opencode into ~/.opencode/bin
# (https://opencode.ai/v2/install, INSTALL_DIR); grok into ~/.grok/bin (https://x.ai/cli/install.sh, BIN_DIR).
PATH_EXTRA = "$HOME/.local/bin:$HOME/bin:$HOME/.opencode/bin:$HOME/.grok/bin"


def extra_dirs(paths: Paths) -> List[Path]:
    """PATH_EXTRA with $HOME resolved to `paths.home`: the same dirs the probe adds to PATH."""
    return [paths.home / part[len("$HOME/"):] for part in PATH_EXTRA.split(":")]


def installed_here(name: str, paths: Paths, which: Optional[Callable[[str], object]] = None) -> bool:
    """On PATH or in one of the PATH_EXTRA dirs (where the vendors' installers put it, also before a new
    shell has picked up their PATH change). `which` defaults to `shutil.which`, looked up at call time."""
    binary = BINARIES[name]
    return bool((which or shutil.which)(binary)) or any((d / binary).exists() for d in extra_dirs(paths))


READY, INSTALLED, MISSING = "ready", "installed", "missing"
STATE_TEXT: Dict[str, str] = {READY: "Ready", INSTALLED: "Installed, not signed in", MISSING: "Missing"}
MARK = "moshi-cockpit-probe"

# One static sh test per harness: exit 0 = signed in. "" = no documented check, the state is unknown.
SIGNIN: Dict[str, str] = {
    # https://code.claude.com/docs/en/cli-reference (checked 2026-11-18): `claude auth status` "Exits with
    # code 0 if logged in, 1 if not". Only on any other exit code (a CLI that cannot answer) the fallback
    # counts: existence of the credentials file Claude Code writes on Linux and on a locked macOS Keychain
    # (https://code.claude.com/docs/en/authentication, "Credential management").
    "claude": 'claude auth status; c=$?; test "$c" -eq 0 || { test "$c" -ne 1 && '
              'test -f "${CLAUDE_CONFIG_DIR:-$HOME/.claude}/.credentials.json"; }',
    # https://learn.chatgpt.com/docs/developer-commands?surface=cli (checked 2026-11-18): `codex login
    # status` "exits with 0 when credentials are present".
    "codex": "codex login status",
    # https://docs.mistral.ai/vibe/code/cli/install-setup (checked 2026-11-18): the key goes to the OS
    # keyring, or to ~/.vibe/.env without one; VIBE_HOME moves that dir (mistral-vibe source,
    # vibe/core/paths/_vibe_home.py). A key in the keyring is not seen: shown as not signed in.
    "vibe": 'test -f "${VIBE_HOME:-$HOME/.vibe}/.env"',
    # https://antigravity.google/docs/install?tab=cli (checked 2026-11-18): credentials only in the OS
    # keyring, no status command, no file: unknown.
    "agy": "",
    # https://x.ai/cli/install.sh (checked 2026-11-18, header): "~/.grok/auth.json from `grok login`".
    "grok": 'test -f "$HOME/.grok/auth.json"',
    # https://opencode.ai/docs/cli/providers/ (checked 2026-11-18): V2 keeps credentials in its SQLite
    # database; auth.json is legacy, `opencode auth list` prints accounts and documents no exit code: unknown.
    "opencode": "",
}

# The visible sign-in command per harness (checked 2026-11-18; the machine may be remote, so no flow that
# needs a browser on the same host).
LOGIN: Dict[str, Tuple[str, ...]] = {
    "claude": ("claude", "auth", "login"),  # https://code.claude.com/docs/en/cli-reference
    # https://learn.chatgpt.com/docs/auth: on remote or headless machines "prefer device code authentication";
    # works on this Mac too. Needs device code login enabled in the ChatGPT security settings (codex says so).
    "codex": ("codex", "login", "--device-auth"),
    "vibe": ("vibe", "--setup"),  # https://docs.mistral.ai/vibe/code/cli/install-setup
    "agy": ("agy",),  # https://antigravity.google/docs/install?tab=cli: first start signs in (SSH: URL + code)
    # https://docs.x.ai/build/cli/reference: "`--device-auth` uses device-code authentication for headless
    # or remote environments"
    "grok": ("grok", "login", "--device-auth"),
    "opencode": ("opencode", "auth", "login"),  # https://opencode.ai/docs/cli/providers/
}


@dataclass(frozen=True)
class Probe:
    states: Dict[str, str]
    has_curl: bool


def _probe_block(name: str) -> str:
    binary, check = BINARIES[name], SIGNIN[name]
    found = f"printf '{MARK}\\t{name}\\t1\\t%s\\n'"
    if check:
        signed = (f"  if {{ {check}; }} >/dev/null 2>&1 </dev/null; then s=1; else s=0; fi\n"
                  f'  {found} "$s"\n')
    else:
        signed = f"  {found} '?'\n"
    return (f"if command -v {binary} >/dev/null 2>&1; then\n{signed}"
            f"else\n  printf '{MARK}\\t{name}\\t0\\t0\\n'\nfi\n")


def probe_script() -> str:
    """The whole probe, built from constants only."""
    curl = (f"if command -v curl >/dev/null 2>&1; then printf '{MARK}\\tneed\\tcurl\\t1\\n'; "
            f"else printf '{MARK}\\tneed\\tcurl\\t0\\n'; fi\n")
    return f'PATH="$PATH:{PATH_EXTRA}"\nexport PATH\n' + "".join(_probe_block(n) for n in KNOWN) + curl + "exit 0\n"


def parse_probe(stdout: str) -> Probe:
    """Only lines that start with MARK and have the exact shape count; everything else (MOTD, profile) is noise."""
    states = {name: MISSING for name in KNOWN}
    has_curl = False
    for raw in stdout.splitlines():
        parts = raw.rstrip("\r").split("\t")
        if len(parts) != 4 or parts[0] != MARK:
            continue
        _, name, installed, signed = parts
        if name == "need":
            if installed == "curl" and signed in ("0", "1"):
                has_curl = signed == "1"
            continue
        if name not in states or installed not in ("0", "1") or signed not in ("0", "1", "?"):
            continue
        states[name] = MISSING if installed == "0" else (READY if signed == "1" else INSTALLED)
    return Probe(states=states, has_curl=has_curl)


def held_argv(command: str) -> List[str]:
    """A visible tab that stays open until Enter. Only ever called with constants from INSTALL, LOGIN, BINARIES."""
    return ["/bin/sh", "-lc",
            f'PATH="$PATH:{PATH_EXTRA}"; {command}; code=$?; '
            'printf "\\nFinished (exit %s). Press Enter to close.\\n" "$code"; read -r _']
