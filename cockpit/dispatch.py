"""`moshi-cockpit dispatch <agent> <message id>`: an orchestrating agent starts another agent for a task it has
already sent through moshi, or wakes it when it runs.

- The message is checked like `ring` does: with the caller's own agent token, it must exist and be addressed to
  the agent. Only an agent with an identity of its own can dispatch.
- The agent runs already: without --model/--effort it is rung; with them dispatch refuses, it never ends an agent.
- The agent does not run: it starts through herdr-core as the caller's child that outlives it, in the caller's
  Herdr space (a visible tab) unless --hidden. hc gives a first message only to templates, after the program is
  ready, so dispatch writes a short-lived template with the model and effort arguments, starts it with the
  doorbell as its task and removes it again. A started program gets herdr-core's environment, never the caller's.
"""

from __future__ import annotations

import os
import re
import secrets
import shlex
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Dict, List, Mapping, Optional

from cockpit import ring as ring_mod
from cockpit import roster as roster_mod
from cockpit.config import load_config
from cockpit.harnesses.base import TOKEN_ENV
from cockpit.hc import Hc
from cockpit.mesh_files import TEMPLATE_ARGS, templates_dir
from cockpit.paths import Paths
from cockpit.registry import find
from cockpit.status import Mesh
from cockpit.text import clean

EFFORTS = ("low", "medium", "high", "xhigh", "max")
TEMPLATE_PREFIX = ".dispatch-"
TEMPLATE_MARK = "short-lived moshi-cockpit dispatch template"
JOB = re.compile(r"[a-z0-9][a-z0-9_-]{0,31}")
HC_ADDRESS = "COCKPIT_HC_ADDRESS"
SPACE_LABEL = "herdr.space"


def _codex_effort(effort: str) -> List[str]:
    return ["-c", f'model_reasoning_effort="{effort}"']


# How each harness takes a model and an effort. None: it has no such switch.
MODEL_FLAG = {"claude": "--model", "agy": "--model", "codex": "-m", "grok": "-m", "vibe": "--model"}
EFFORT_ARGS: Dict[str, Optional[Callable[[str], List[str]]]] = {
    "claude": lambda e: ["--effort", e], "agy": lambda e: ["--effort", e], "codex": _codex_effort,
    "grok": lambda e: ["--reasoning-effort", e], "vibe": None}


@dataclass(frozen=True)
class Outcome:
    address: str
    action: str  # "started" or "rang"


def harness_args(harness: str, model: str = "", effort: str = "") -> List[str]:
    """The arguments for a model and an effort; ValueError for what the harness cannot take."""
    args: List[str] = []
    if model:
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:/-]{0,127}", model):
            raise ValueError("a model name has letters, digits and . _ : / - only")
        args += [MODEL_FLAG[harness], model]
    if effort:
        if effort not in EFFORTS:
            raise ValueError(f"--effort is one of {', '.join(EFFORTS)}")
        translate = EFFORT_ARGS.get(harness)
        if translate is None:
            raise ValueError(f"{harness} has no effort setting; leave out --effort")
        args += translate(effort)
    return args


def default_job(name: str) -> str:
    return re.sub(r"[^a-z0-9_-]", "-", name.lower())[:32].strip("-") or "agent"


def template_text(paths: Paths, name: str, harness: str, extra: List[str]) -> str:
    command = " ".join(shlex.quote(part) for part in
                       [str(paths.local_bin / "moshi-cockpit"), "run", name, *TEMPLATE_ARGS.get(harness, []), *extra])
    return "\n".join(["---", f"description: {name} ({harness}), {TEMPLATE_MARK}.", f"command: {command}", "---",
                      "Read and follow the agent-mesh skill (~/.agents/skills/agent-mesh/SKILL.md) before you act.",
                      "The line below is a doorbell: load that moshi message with mesh_get. It is your task."]) + "\n"


def caller_terminal(environ: Mapping[str, str]) -> str:
    """The caller's herdr-core address on this machine: from `run`'s COCKPIT_HC_ADDRESS, else HC_TERMINAL_ID."""
    address = environ.get(HC_ADDRESS, "").split("@", 1)[0]
    return address or environ.get("HC_TERMINAL_ID", "")


def dispatch(paths: Paths, name: str, message_id: str, hc: Hc, model: str = "", effort: str = "", job: str = "",
             hidden: bool = False, environ: Mapping[str, str] = os.environ,
             fetch: ring_mod.Fetch = ring_mod.fetch_message, token_bytes: int = 3) -> Outcome:
    agent = find(paths, name)  # an agent of this machine, with its exact name
    extra = harness_args(agent.harness, model, effort)
    job = job or default_job(agent.name)
    if not JOB.fullmatch(job):
        raise ValueError("--name has 1 to 32 of a-z 0-9 - _ and starts with a letter or digit")
    message = _checked_message(paths, agent.name, message_id, environ, fetch)
    here = roster_mod.build(hc.terminals(), {}, Mesh(), roster_mod.MOSHI_OFF, "")
    running = [row for row in here.running if row.name.lower() == agent.name.lower()]
    if running:
        if extra:
            raise ValueError(f"{agent.name} runs already in {running[0].address}; end it first, or dispatch without "
                             "--model/--effort to ring it")
        return Outcome(ring_mod.paste(hc, agent.name, ring_mod.doorbell(message_id, message)), "rang")
    caller = caller_terminal(environ)
    labels = [] if hidden else _space_labels(hc, caller)
    template = templates_dir(paths) / f"{TEMPLATE_PREFIX}{job}-{secrets.token_hex(token_bytes)}.md"
    _write_new(template, template_text(paths, agent.name, agent.harness, extra))
    try:
        answer = hc.run_template(template.stem, ring_mod.doorbell(message_id, message), job, labels, parent=caller)
    finally:
        template.unlink()
    address = answer["address"]
    first = answer.get("first_message") if isinstance(answer.get("first_message"), dict) else {}
    if not first.get("sent"):
        raise ValueError(f"{agent.name} started in {address}, but its task did not arrive "
                         f"({clean(first.get('reason'), 'no reason given', limit=120)}); read it with hc read {address}")
    return Outcome(address, "started")


def _checked_message(paths: Paths, name: str, message_id: str, environ: Mapping[str, str],
                     fetch: ring_mod.Fetch) -> Dict:
    if not ring_mod.MESSAGE_ID.fullmatch(message_id or ""):
        raise ValueError("a message id looks like msg_ followed by 26 characters")
    config = load_config(paths)
    if not config.has("moshi"):
        raise ValueError("moshi is not set up; dispatch needs a message in moshi")
    token = environ.get(TOKEN_ENV["moshi"], "")
    if not token:
        raise ValueError(ring_mod.NO_IDENTITY)
    message = fetch(config.servers["moshi"], token, message_id)
    if str(message.get("to") or "").lower() != name.lower():
        raise ValueError(f"{message_id} is not addressed to {name}")
    return message


def _space_labels(hc: Hc, caller: str) -> List[str]:
    """The caller's Herdr space, so the new terminal shows as a tab beside it; none when it is not known."""
    terminal = hc.terminal(caller) if caller else None
    space = terminal.labels.get(SPACE_LABEL, "") if terminal else ""
    return [f"{SPACE_LABEL}={space}"] if re.fullmatch(r"[A-Za-z0-9_-]{1,128}", space) else []


def _write_new(path: Path, text: str) -> None:
    """Creates the template; refuses to touch a file that is there already (O_EXCL)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(str(path), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        handle.write(text)
