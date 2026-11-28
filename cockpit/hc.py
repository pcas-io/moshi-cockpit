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
# Foreground programs that are a shell, not an agent: a label left on such a terminal belongs to an agent that
# has ended, and text pasted there would run as a command.
SHELLS = frozenset({"sh", "bash", "zsh", "fish", "dash", "ksh", "tcsh", "csh", "nu", "pwsh", "elvish", "xonsh"})

Runner = Callable[[Sequence[str], int], Tuple[int, str, str]]


class HcError(RuntimeError):
    """hc is missing, or answered with an error. The message is safe to print."""


@dataclass(frozen=True)
class Terminal:
    address: str
    app: str  # what hc says runs in the foreground: claude-code, codex, zsh, ...
    status: str  # the agent's own report (idle, working, done, blocked, error), "" for plain programs
    labels: Dict[str, str] = field(default_factory=dict)

    @property
    def agent_running(self) -> bool:
        return bool(self.app) and self.app not in SHELLS

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

    def _call(self, *args: str) -> Any:
        code, out, err = self._runner([self._binary, *args, "--json"], TIMEOUT)
        if code != 0:
            raise HcError(f"hc {args[0]}: {_error_text(out, err)}")
        try:
            return json.loads(out) if out.strip() else {}
        except ValueError:
            raise HcError(f"hc {args[0]}: answer is not JSON") from None

    def terminals(self) -> List[Terminal]:
        """This machine's live terminals (no `machine/t7` addresses), each with its labels."""
        listed = self._call("ls").get("terminals") or []
        found = []
        for row in listed:
            address = row.get("address") if isinstance(row, dict) else None
            if not isinstance(address, str) or not address or "/" in address:
                continue
            details = self._call("get", address)
            labels = details.get("labels") if isinstance(details, dict) else None
            found.append(Terminal(address=address, app=_text(row.get("agent")), status=_status(row.get("status")),
                                  labels={k: v for k, v in (labels or {}).items()
                                          if isinstance(k, str) and isinstance(v, str)}))
        return found

    def label(self, address: str, key: str, value: str) -> None:
        self._call("label", address, f"{key}={value}")

    def send(self, address: str, text: str) -> None:
        """Pastes one line and presses Enter. hc itself refuses a terminal that waits for a person (blocked)."""
        self._call("send", address, text)


def _text(value: Any) -> str:
    return clean(value, "", limit=64) if isinstance(value, str) else ""


def _status(value: Any) -> str:
    if isinstance(value, dict):  # `hc get` shape: {"app": ..., "state": ...}
        value = value.get("state")
    return _text(value)


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
