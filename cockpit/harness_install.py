"""`moshi-cockpit harness install <name>`: install a missing agent CLI with the vendor's own installer.

Nothing runs without a typed yes, and the exact command is shown first. The commands are the vendors'
documented macOS installers (checked 2026-11-17, opencode 2026-11-18); each is the vendor's own
pipe-to-shell line, run as `/bin/sh -c` exactly as published. Never add a command here that a vendor
does not document.
"""

from __future__ import annotations

import shutil
import subprocess
from dataclasses import dataclass
from typing import Callable, Dict, Optional, Sequence, Tuple

from cockpit.inventory import KNOWN, installed_here
from cockpit.paths import Paths


@dataclass(frozen=True)
class InstallSpec:
    argv: Tuple[str, ...]  # empty: the vendor has no scriptable installer, `source_url` is the way
    source_url: str
    needs: Tuple[str, ...] = ()  # programs that must exist before the installer can run


def _script(command: str) -> Tuple[str, ...]:
    return ("/bin/sh", "-c", command)


INSTALL: Dict[str, InstallSpec] = {
    # source_url (checked 2026-11-17): https://code.claude.com/docs/en/setup ("Native Install", macOS)
    "claude": InstallSpec(_script("curl -fsSL https://claude.ai/install.sh | bash"),
                          "https://code.claude.com/docs/en/setup", ("curl",)),
    # source_url (checked 2026-11-17): https://github.com/openai/codex (README, install script)
    "codex": InstallSpec(_script("curl -fsSL https://chatgpt.com/codex/install.sh | sh"),
                         "https://github.com/openai/codex", ("curl",)),
    # source_url (checked 2026-11-17): https://github.com/mistralai/mistral-vibe (README, macOS)
    "vibe": InstallSpec(_script("curl -LsSf https://mistral.ai/vibe/install.sh | bash"),
                        "https://github.com/mistralai/mistral-vibe", ("curl",)),
    # source_url (checked 2026-11-17): https://antigravity.google/docs/getting-started (CLI tab)
    "agy": InstallSpec(_script("curl -fsSL https://antigravity.google/cli/install.sh | bash"),
                       "https://antigravity.google/docs/getting-started", ("curl",)),
    # source_url (checked 2026-11-17): https://docs.x.ai/build/overview
    "grok": InstallSpec(_script("curl -fsSL https://x.ai/cli/install.sh | bash"),
                        "https://docs.x.ai/build/overview", ("curl",)),
    # source_url (checked 2026-11-18): https://opencode.ai/docs (install section; the script installs
    # `opencode` into ~/.opencode/bin, no sudo)
    "opencode": InstallSpec(_script("curl -fsSL https://opencode.ai/v2/install | bash"),
                            "https://opencode.ai/docs", ("curl",)),
}

Runner = Callable[[Sequence[str]], int]


@dataclass(frozen=True)
class InstallIO:
    ask: Callable[[str], str] = input
    say: Callable[[str], None] = print
    interactive: bool = True


def shown_command(spec: InstallSpec) -> str:
    """The command as the vendor prints it: the script itself for `sh -c`, else the joined argv."""
    if len(spec.argv) == 3 and spec.argv[1] == "-c":
        return spec.argv[2]
    return " ".join(spec.argv)


def run_argv(argv: Sequence[str]) -> int:
    """The real runner: output goes straight to the terminal, the installer may ask or print on its own."""
    try:
        return subprocess.call(list(argv))
    except OSError:
        return 127


def install(name: str, io: InstallIO, runner: Runner, paths: Paths,
            which: Callable[[str], object] = shutil.which) -> int:
    spec = INSTALL.get(name)
    if spec is None:
        io.say(f"No installer is known for {name}.")
        return 1
    if installed_here(name, paths):
        io.say(f"{name} is already installed.")
        return 0
    if not spec.argv:
        io.say(f"Install {name} from {spec.source_url}, then reopen this dialog.")
        return 1
    missing = [tool for tool in spec.needs if not which(tool)]
    if missing:
        io.say(f"{name} needs {', '.join(missing)} to install, and it is not found. Install it first, then try again.")
        return 1
    io.say(f"This runs the official installer of {name}: {shown_command(spec)}")
    if not io.interactive:
        io.say("Not run: this needs a terminal to confirm. Run the command above yourself, or open a terminal.")
        return 1
    try:
        answer = io.ask("Continue? [y/N] ")
    except EOFError:  # input closed: that is a no
        answer = ""
    if answer.strip().lower() not in ("y", "yes"):
        io.say("Cancelled, nothing was installed.")
        return 1
    code = runner(spec.argv)
    if not installed_here(name, paths):
        reason = f"the installer exited with code {code}" if code else "the installer finished, but the binary is not on your PATH yet"
        io.say(f"{name} is not installed: {reason}. Open a new terminal and retry, or follow {spec.source_url}")
        return 1
    io.say(f"{name} is installed.")
    return 0


def run_cli(name: str, paths: Paths, io: Optional[InstallIO] = None) -> int:
    if name not in INSTALL:
        raise ValueError(f"Unknown harness '{name}' (known: {', '.join(KNOWN)})")
    return install(name, io or InstallIO(), run_argv, paths)
