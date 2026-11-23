"""`moshi-cockpit agent add`: create an agent, guided or with flags.

Without arguments the wizard asks for name, harness and role. With moshi set up the agent is created there as
well (agents_remote.create); without moshi it stays local: name and harness, nothing online. With name and
--harness it runs without questions.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, List, Optional

from cockpit import agents_remote
from cockpit.agents import AgentContext
from cockpit.agents_remote import check_harness
from cockpit.config import load_config
from cockpit.harnesses import HARNESSES
from cockpit.paths import Paths
from cockpit.registry import load_agents, validate_name


@dataclass(frozen=True)
class WizardIO:
    ask: Callable[[str], str] = input
    say: Callable[[str], None] = print


def harness_choices(paths: Paths) -> List[str]:
    """Harnesses that can take a new agent: installed, and single-instance ones only while free."""
    taken = {a.harness for a in load_agents(paths, enforce=False)}
    return [name for name, h in HARNESSES.items()
            if h.installed(paths) and not h.no_own_agents and not (h.single_instance and name in taken)]


def unavailable(paths: Paths) -> List[str]:
    """Why a harness is not offered, one line each ("per machine" on a Linux machine, "per Mac" here)."""
    agents = load_agents(paths, enforce=False)
    here = "machine" if paths.headless else "Mac"
    lines = []
    for name, h in HARNESSES.items():
        if h.no_own_agents:
            lines.append(f"{name}: {h.no_own_agents}")
            continue
        if not h.installed(paths):
            lines.append(f"{name}: not installed ({h.binary})")
            continue
        taken = [a.name for a in agents if a.harness == name]
        if h.single_instance and taken:
            lines.append(f"{name}: one agent per {here}, taken by '{taken[0]}'")
    return lines


def harness_offer(paths: Paths) -> List[dict]:
    """For the dialog's "+ Agent": every harness, whether it is installed, whether it can take a new agent,
    why not, and whether it connects to the servers with tokens of its own."""
    free = set(harness_choices(paths))
    reasons = {line.split(":", 1)[0]: line.split(":", 1)[1].strip() for line in unavailable(paths)}
    return [{"name": name, "installed": h.installed(paths), "available": name in free, "reason": reasons.get(name, ""),
             "native_mesh": h.native_mesh} for name, h in HARNESSES.items()]


def skill_note(ctx: AgentContext, skill: str) -> List[str]:
    if skill and not load_config(ctx.paths).has("ocellio"):
        return ["The skill is ignored: there is no ocellio set up here"]
    return []


def create(ctx: AgentContext, name: str, harness: str, role: str = "", skill: str = "") -> List[str]:
    """Creates the agent (in moshi too, when set up) and says what follows, one note each."""
    agent = agents_remote.create(ctx, name, harness, role, skill)
    has_moshi = load_config(ctx.paths).has("moshi")
    notes = [f"Agent {agent.name} ({harness}) created" + (" in moshi and here" if "moshi" in agent.bound else "")]
    if not has_moshi:
        notes.append("Local only: there is no moshi set up here, so there is no moshi identity")
    return notes + agents_remote.notes(ctx, agent) + skill_note(ctx, skill)


def _ask_name(ctx: AgentContext, io: WizardIO) -> str:
    existing = {a.name.lower() for a in load_agents(ctx.paths)}
    for _ in range(3):
        answer = io.ask("Name of the agent (e.g. research): ").strip()
        try:
            name = validate_name(answer)
        except ValueError as exc:
            io.say(f"   {exc}")
            continue
        if name.lower() in existing:
            io.say(f"   There is already an agent '{name}'")
            continue
        return name
    raise ValueError("No valid name entered, cancelled")


def _ask_harness(ctx: AgentContext, io: WizardIO) -> str:
    choices = harness_choices(ctx.paths)
    if not choices:
        raise ValueError("No harness available: none is installed, or agy has its one agent already")
    for i, name in enumerate(choices, 1):
        io.say(f"   {i}. {name}")
    for line in unavailable(ctx.paths):
        io.say(f"      not offered: {line}")
    for _ in range(3):
        answer = io.ask(f"Harness (1–{len(choices)} or name): ").strip().lower()
        if answer.isdigit() and 1 <= int(answer) <= len(choices):
            return choices[int(answer) - 1]
        if answer in choices:
            return answer
        io.say("   Please choose one of the harnesses shown")
    raise ValueError("No harness chosen, cancelled")


def run(ctx: AgentContext, io: WizardIO = WizardIO(), name: Optional[str] = None, harness: Optional[str] = None,
        role: Optional[str] = None, skill: str = "") -> List[str]:
    """The guided path: asks for whatever is missing, then creates the agent."""
    name = validate_name(name) if name else _ask_name(ctx, io)
    if harness:
        check_harness(ctx, harness)
    else:
        harness = _ask_harness(ctx, io)
    if role is None:
        role = io.ask("Role in a few words (may stay empty): ").strip()
    return create(ctx, name, harness, role, skill) + [start_hint(name)]


def start_hint(name: str) -> str:
    return f'Start it from the "+" menu or with moshi-cockpit run {name}'
