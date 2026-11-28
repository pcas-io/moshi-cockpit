"""`moshi-cockpit run <agent> [args…]`: starts a registered agent through its harness.

One start path for every agent and every harness: the harness builds argv and environment, tokens go
into the environment only, never argv. For harnesses that ask for it the environment is cleaned first
(no herdr/hc control, no SSH agent). That is not isolation: the agent still runs as the same user. The
process is replaced (execve), so herdr sees the agent CLI itself.

Before the start, `claim` makes this installation the agent's active one in moshi: moshi's MCP endpoint
refuses an agent token whose installation is not the active one, a freshly loaded agent included. `prepare`
runs first (command, tokens, binary), so a start that cannot happen never takes an agent over.
"""

from __future__ import annotations

import os
import shutil
import sys
from dataclasses import dataclass
from typing import Callable, Dict, List, Mapping, Optional, Sequence, Tuple

from cockpit import agents_remote, status
from cockpit.config import load_config
from cockpit.harnesses import HARNESSES, Ctx
from cockpit.harnesses.base import DROPPED_ENV, DROPPED_ENV_PREFIXES, TOKEN_ENV_PREFIXES
from cockpit.hc import AGENT_LABEL, Hc, HcError, find_binary
from cockpit.machines import run_capture
from cockpit.moshi_api import MoshiApi, MoshiError
from cockpit.roster import machine_name
from cockpit.secrets import SecretStore
from cockpit.paths import Paths, keychain_service
from cockpit.registry import find
from cockpit.tokens import moshi_token

Exec = Callable[[str, List[str], dict], None]
SHELLS = ("zsh", "bash")
RUN_MARKER = "MOSHI_COCKPIT_RUN"
HC_TERMINAL = "HC_TERMINAL_ID"  # herdr-core's address of the terminal this start runs in
HC_ADDRESS = "COCKPIT_HC_ADDRESS"  # what the agent gets instead: survives the hygiene that drops HC_*
UNREACHABLE = "moshi is not reachable; starting without the mesh."
TAKEN_IN_BETWEEN = "{name} was held by {label} until just now: it was started there after the check, and runs here now."


class ActiveElsewhere(RuntimeError):
    """Another installation holds the agent and no takeover was asked for (exit code 4)."""

    def __init__(self, label: str) -> None:
        super().__init__(f"active on {label}")
        self.label = label


def _warn(text: str) -> None:
    print(text, file=sys.stderr)


def claim(paths: Paths, keychain: SecretStore, name: str, take_over: bool = False,
          api: Optional[MoshiApi] = None, warn: Callable[[str], None] = _warn,
          confirm: Optional[Callable[[str], bool]] = None) -> None:
    """Makes this installation the agent's active one, silently when nobody or this one holds it. Another
    installation holds it: ActiveElsewhere, unless `take_over` or `confirm(label)` says yes. Nothing to do
    without moshi, without the agent's moshi token, or for an older token bound to no installation. moshi unreachable: one warning,
    the agent starts anyway. Without a usable device key ApprovalRequired; a token moshi refuses: ValueError,
    so no harness starts only to be refused."""
    agent = find(paths, name)
    config = load_config(paths)
    harness = HARNESSES[agent.harness]
    if ("moshi" not in harness.servers(Ctx(paths=paths, config=config, keychain=keychain))
            or not keychain.exists(keychain_service("moshi", agent.name), agent.name)):
        return
    url = config.servers["moshi"]
    api = api or agents_remote.api_for(url)
    try:
        holder = status.where(paths, config, keychain, agent.name, api)
        if holder["active"] and not holder["here"] and not take_over:
            if not (confirm and confirm(holder["active"])):
                raise ActiveElsewhere(holder["active"])
        previous = api.activate(moshi_token(paths, keychain, agent.name, url))
        if previous is not None and previous not in (holder["active"], holder.get("own")):
            warn(TAKEN_IN_BETWEEN.format(name=agent.name, label=previous))
    except MoshiError as exc:
        if status.unreachable(exc):
            warn(UNREACHABLE)
        elif exc.reason != "legacy_token":
            raise (ValueError(agents_remote.DEAD_TOKEN.format(name=agent.name)) if exc.status == 401
                   else agents_remote.refusal(exc, agent.name)) from None


def agent_env(base: Mapping[str, str], extra: Dict[str, str], clean: bool) -> Dict[str, str]:
    env = {k: v for k, v in base.items() if not k.startswith(TOKEN_ENV_PREFIXES)}
    if clean:
        for key in list(env):
            if key.startswith(DROPPED_ENV_PREFIXES) or key in DROPPED_ENV:
                env.pop(key)
    env.update(extra)
    return env


def command(paths: Paths, keychain: SecretStore, name: str, extra: Sequence[str]):
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


@dataclass(frozen=True)
class Start:
    """What `execute` replaces this process with: the program, its argv and its environment."""
    path: str
    argv: List[str]
    env: Dict[str, str]


NOT_FOUND = "{binary} is not found on your PATH"


def prepare(paths: Paths, keychain: SecretStore, name: str, extra: Sequence[str],
            environ: Mapping[str, str] = os.environ, on_terminal: Callable[[], bool] = _on_terminal) -> Start:
    """Everything a start needs, tokens and the binary included, before anything changes in moshi: a start
    that cannot happen never takes an agent over."""
    harness, argv, extra_env = command(paths, keychain, name, extra)
    binary = shutil.which(harness.binary)
    if not binary:
        raise ValueError(NOT_FOUND.format(binary=harness.binary))
    env = agent_env(environ, extra_env, harness.clean_env)
    terminal = environ.get(HC_TERMINAL, "")
    if terminal:  # where the agent can be rung; it reports this in working_on (presence rule)
        env[HC_ADDRESS] = f"{terminal}@{machine_name()}"
    shell = interactive_shell(environ) if harness.shell_start and on_terminal() else None
    if shell:
        # Arguments stay positional ("$@"): nothing a person typed is ever parsed by the shell. The marker
        # tells a wrapper that these arguments are ours, so it may still ask what it asks at a plain start.
        return Start(shell, [shell, "-ic", f'{harness.binary} "$@"', harness.binary, *argv[1:]], {**env, RUN_MARKER: "1"})
    return Start(binary, list(argv), env)


def label_terminal(paths: Paths, name: str, environ: Mapping[str, str] = os.environ,
                   hc_for: Optional[Callable[[str], Hc]] = None, warn: Callable[[str], None] = _warn) -> None:
    """Labels this terminal `agent=<name>` in herdr-core, so the roster knows who runs here and `ring` can reach
    the agent. Outside a herdr-core terminal, or without hc, nothing happens; an hc error is one warning, the
    start goes on."""
    terminal = environ.get(HC_TERMINAL, "")
    # Only herdr-core's own install places under this HOME, never PATH: a start must not reach a core that
    # belongs to another HOME (a test's temporary one included).
    binary = find_binary(paths, which=lambda program: None) if terminal else None
    if not binary:
        return
    try:
        (hc_for or Hc)(binary).label(terminal, AGENT_LABEL, find(paths, name).name)
    except HcError as exc:
        warn(f"{exc}; other agents cannot ring {name} here.")


def running_elsewhere(paths: Paths, name: str, environ: Mapping[str, str] = os.environ,
                      hc_for: Optional[Callable[[str], Hc]] = None) -> str:
    """The terminal where `name` runs already, other than this one; "" when none does or hc is not here.
    Like label_terminal, only herdr-core's own install places under this HOME are used, never PATH."""
    binary = find_binary(paths, which=lambda program: None)
    if not binary:
        return ""
    try:
        terminals = (hc_for or Hc)(binary).terminals()
    except HcError:
        return ""
    own = environ.get(HC_TERMINAL, "")
    agent = find(paths, name)
    for terminal in terminals:
        if terminal.runs(agent.name, agent.harness) and terminal.address != own:
            return terminal.address
    return ""


def focus(paths: Paths, address: str, runner: Callable[..., Tuple[int, str, str]] = run_capture) -> bool:
    """Shows that terminal in the Herdr window (herdr-app agent.focus); False without herdr-app or on an error."""
    app = paths.local_bin / "herdr-app"
    if not app.exists():
        return False
    code, _, _ = runner([str(app), "do", "agent.focus", "--terminal", address], 15)
    return code == 0


def execute(start: Start, execve: Optional[Exec] = None) -> None:
    (execve or os.execve)(start.path, start.argv, start.env)


def run_agent(paths: Paths, keychain: SecretStore, name: str, extra: Sequence[str],
              execve: Exec = os.execve, environ: Mapping[str, str] = os.environ,
              on_terminal: Callable[[], bool] = _on_terminal) -> None:
    start = prepare(paths, keychain, name, extra, environ, on_terminal)
    label_terminal(paths, name, environ)
    execute(start, execve)
