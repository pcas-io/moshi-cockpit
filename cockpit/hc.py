"""The few herdr-core (`hc`) calls the mesh needs: list this machine's terminals with their labels, label one,
and paste one line into one. Nothing here presses keys or reads a screen.

Without the HC_* environment hc still reaches the core over its default socket, so leaving those variables out
of an agent's environment is hygiene, not a boundary. That is why agents ring through `moshi-cockpit ring`, which
only ever pastes a doorbell line into a terminal that runs a named agent, instead of using hc themselves.
"""

from __future__ import annotations

import json
import shutil
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

from cockpit.machines import run_capture
from cockpit.paths import Paths
from cockpit.text import clean

AGENT_LABEL = "agent"
TIMEOUT = 15
START_TIMEOUT = 150  # hc run waits until the program is ready (up to a minute, with retries) before it answers
# What herdr-core reports as the app of each harness: a label counts only while its agent's own program runs.
HARNESS_APPS = {"claude": {"claude-code", "claude"}, "agy": {"antigravity", "agy"}, "codex": {"codex"},
                "grok": {"grok"}, "vibe": {"vibe", "mistral-vibe"}}
# An agent counts as running only while it reports its own status to herdr-core (OSC 7501): hc clears that
# report when the terminal's shell returns to the foreground. A report that names a shell is refused as well.
SHELLS = frozenset({"sh", "bash", "zsh", "fish", "dash", "ksh", "tcsh", "csh", "nu", "pwsh", "elvish", "xonsh"})

Runner = Callable[[Sequence[str], int], Tuple[int, str, str]]


class HcError(ValueError):
    """hc is missing, or answered with an error. The message is safe to print."""


@dataclass(frozen=True)
class Terminal:
    address: str
    app: str  # the agent that reports its status here (claude-code, codex, ...); "" when none does
    status: str  # its report: idle, working, done, blocked, error
    labels: Dict[str, str] = field(default_factory=dict)

    @property
    def agent_running(self) -> bool:
        name = self.app.rsplit("/", 1)[-1].lstrip("-").lower()
        return bool(name) and name not in SHELLS

    @classmethod
    def from_get(cls, address: str, details: Any) -> "Terminal":
        details = details if isinstance(details, dict) else {}
        report = details.get("status") if isinstance(details.get("status"), dict) else {}
        labels = details.get("labels") if isinstance(details.get("labels"), dict) else {}
        return cls(address=address, app=_text(report.get("app")), status=_text(report.get("state")),
                   labels={k: v for k, v in labels.items() if isinstance(k, str) and isinstance(v, str)})

    def runs(self, name: str, harness: str) -> bool:
        """This terminal runs the agent `name` with its own program (not something else under a stale label)."""
        app = self.app.rsplit("/", 1)[-1].lstrip("-").lower()
        return (self.agent_running and self.agent_name.lower() == name.lower()
                and app in HARNESS_APPS.get(harness, {harness}))

    @property
    def agent_name(self) -> str:
        return self.labels.get(AGENT_LABEL, "")


def find_binary(paths: Paths, which: Callable[[str], Optional[str]] = shutil.which) -> Optional[str]:
    for candidate in (paths.herdr_core_bin, paths.local_bin / "hc"):
        if candidate.exists():
            return str(candidate)
    return which("hc") or which("herdr-core")


class Hc:
    def __init__(self, binary: str, runner: Runner = run_capture) -> None:
        self._binary = binary
        self._runner = runner

    def _call(self, *args: str, timeout: int = TIMEOUT) -> Any:
        code, out, err = self._runner([self._binary, *args, "--json"], timeout)
        if code != 0:
            raise HcError(f"hc {args[0]}: {_error_text(out, err)}")
        try:
            return json.loads(out) if out.strip() else {}
        except ValueError:
            raise HcError(f"hc {args[0]}: answer is not JSON") from None

    def terminals(self) -> List[Terminal]:
        """This machine's live terminals (no `machine/t7` addresses), each with its labels and agent report. A
        terminal that ends between the list and its details is left out."""
        answer = self._call("ls")
        listed = answer.get("terminals") if isinstance(answer, dict) else None
        found = []
        for row in listed if isinstance(listed, list) else []:
            address = row.get("address") if isinstance(row, dict) else None
            if not isinstance(address, str) or not address or "/" in address:
                continue
            terminal = self.terminal(address)
            if terminal is not None:
                found.append(terminal)
        return found

    def terminal(self, address: str) -> Optional[Terminal]:
        """One terminal as it is now; None when it is gone."""
        try:
            return Terminal.from_get(address, self._call("get", address))
        except HcError:
            return None

    def run_template(self, template: str, task: str, name: str, labels: Sequence[str] = (),
                     parent: str = "", outlive_parent: bool = True) -> Dict[str, Any]:
        """Starts a template in the background with its first message; hc waits for the program to be ready
        before it sends that message. Returns hc's answer (address, first_message)."""
        args = ["run", "--bg", "--name", name]
        for item in labels:
            args += ["--label", item]
        if parent:
            args += ["--parent", parent]
        if outlive_parent:
            args.append("--outlive-parent")
        answer = self._call(*args, template, task, timeout=START_TIMEOUT)
        if not isinstance(answer, dict) or not isinstance(answer.get("address"), str):
            raise HcError("hc run: no terminal address in its answer")
        return answer

    def label(self, address: str, key: str, value: str) -> None:
        self._call("label", address, f"{key}={value}")

    def send(self, address: str, text: str) -> None:
        """Pastes one line and presses Enter. hc itself refuses a terminal that waits for a person (blocked)."""
        self._call("send", address, text)


def _text(value: Any) -> str:
    return clean(value, "", limit=64) if isinstance(value, str) else ""


def _error_text(out: str, err: str) -> str:
    for text in (out, err):
        try:
            found = json.loads(text)
        except ValueError:
            continue
        error = found.get("error") if isinstance(found, dict) else None
        if isinstance(error, dict) and error.get("message"):
            return clean(error["message"], "?", limit=300)
    return clean((err or out).strip(), "no answer", limit=300)
