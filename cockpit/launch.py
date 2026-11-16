"""`moshi-cockpit run <agent> [args…]`: starts a registered agent through its harness.

One start path for every agent and every harness: the harness builds argv and environment, tokens go
into the environment only, never argv. For harnesses that ask for it the environment is cleaned first
(no herdr/hc control, no SSH agent). That is not isolation: the agent still runs as the same user. The
process is replaced (execve), so herdr sees the agent CLI itself.
"""

from __future__ import annotations

import os
import shutil
import sys
from typing import Callable, Dict, List, Mapping, Optional, Sequence

from cockpit.config import load_config
from cockpit.harnesses import HARNESSES, Ctx
from cockpit.harnesses.base import DROPPED_ENV, DROPPED_ENV_PREFIXES, TOKEN_ENV_PREFIXES
from cockpit.keychain import Keychain
from cockpit.paths import Paths
from cockpit.registry import find

Exec = Callable[[str, List[str], dict], None]
SHELLS = ("zsh", "bash")
RUN_MARKER = "MOSHI_COCKPIT_RUN"


def agent_env(base: Mapping[str, str], extra: Dict[str, str], clean: bool) -> Dict[str, str]:
    env = {k: v for k, v in base.items() if not k.startswith(TOKEN_ENV_PREFIXES)}
    if clean:
        for key in list(env):
            if key.startswith(DROPPED_ENV_PREFIXES) or key in DROPPED_ENV:
                env.pop(key)
    env.update(extra)
    return env


def command(paths: Paths, keychain: Keychain, name: str, extra: Sequence[str]):
    agent = find(paths, name)
    harness = HARNESSES[agent.harness]
    argv, env = harness.command(Ctx(paths=paths, config=load_config(paths), keychain=keychain), agent, extra)
    return harness, argv, env


def interactive_shell(environ: Mapping[str, str]) -> Optional[str]:
    """The person's shell, if it is one whose `-ic` we know; None means: start the CLI directly."""
    shell = environ.get("SHELL", "")
    usable = os.path.isabs(shell) and os.path.basename(shell) in SHELLS and os.access(shell, os.X_OK)
    return shell if usable else None


def _on_terminal() -> bool:
    return sys.stdin.isatty() and sys.stdout.isatty()


def run_agent(paths: Paths, keychain: Keychain, name: str, extra: Sequence[str],
              execve: Exec = os.execve, environ: Mapping[str, str] = os.environ,
              on_terminal: Callable[[], bool] = _on_terminal) -> None:
    harness, argv, extra_env = command(paths, keychain, name, extra)
    binary = shutil.which(harness.binary)
    if not binary:
        raise ValueError(f"{harness.binary} nicht gefunden (PATH)")
    env = agent_env(environ, extra_env, harness.clean_env)
    shell = interactive_shell(environ) if harness.shell_start and on_terminal() else None
    if shell:
        # Arguments stay positional ("$@"): nothing a person typed is ever parsed by the shell. The marker
        # tells a wrapper that these arguments are ours, so it may still ask what it asks at a plain start.
        execve(shell, [shell, "-ic", f'{harness.binary} "$@"', harness.binary, *argv[1:]], {**env, RUN_MARKER: "1"})
    else:
        execve(binary, argv, env)
