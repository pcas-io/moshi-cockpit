"""`moshi-cockpit agent add`: create an agent, guided or with flags.

Without arguments the wizard asks for name, harness and role, then offers to connect the agent to the
configured servers (`bot add`). Without moshi and ocellio it only creates the local agent: name and
harness, nothing online. With name and --harness it runs without questions, as before.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, List, Optional

from cockpit import defaults, registry
from cockpit.agents import AgentContext
from cockpit.config import load_config
from cockpit.harnesses import HARNESSES
from cockpit.paths import Paths
from cockpit.person import identity
from cockpit.registry import Agent, load_agents, validate_name


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
    """Why a harness is not offered, one line each."""
    agents = load_agents(paths, enforce=False)
    lines = []
    for name, h in HARNESSES.items():
        if h.no_own_agents:
            lines.append(f"{name}: {h.no_own_agents}")
            continue
        if not h.installed(paths):
            lines.append(f"{name}: nicht installiert ({h.binary})")
            continue
        taken = [a.name for a in agents if a.harness == name]
        if h.single_instance and taken:
            lines.append(f"{name}: nur ein Agent pro Rechner, belegt durch '{taken[0]}'")
    return lines


def harness_offer(paths: Paths) -> List[dict]:
    """For the dialog's "New agent": every harness, whether it can take a new agent, why not, and whether
    it connects to the servers itself (then `bot add` follows)."""
    free = set(harness_choices(paths))
    reasons = {line.split(":", 1)[0]: line.split(":", 1)[1].strip() for line in unavailable(paths)}
    return [{"name": name, "available": name in free, "reason": reasons.get(name, ""),
             "native_mesh": h.native_mesh} for name, h in HARNESSES.items()]


def check_harness(ctx: AgentContext, harness: str) -> None:
    found = HARNESSES.get(harness)
    if found is None:
        raise ValueError(f"Unbekannte Harness '{harness[:40]}'. Erlaubt: {', '.join(HARNESSES)}")
    if found.no_own_agents:
        raise ValueError(f"{harness} {found.no_own_agents}")
    if found.single_instance:
        taken = [a.name for a in load_agents(ctx.paths) if a.harness == harness]
        if taken:
            raise ValueError(f"{harness} {found.single_reason}: es gibt schon den Agent '{taken[0]}'. "
                             f"Erst moshi-cockpit agent remove {taken[0]}")


def skill_note(ctx: AgentContext, skill: str) -> List[str]:
    if skill and not load_config(ctx.paths).has("ocellio"):
        return ["Skill wird ignoriert: Hier ist kein ocellio eingerichtet"]
    return []


def create(ctx: AgentContext, name: str, harness: str, role: str = "", skill: str = "") -> List[str]:
    check_harness(ctx, harness)
    registry.add(ctx.paths, ctx.backup, Agent(name=name, harness=harness, role=role, skill=skill))
    found = HARNESSES[harness]
    defaults.sync_agent_menu(ctx.paths, ctx.backup)
    notes = [f"Agent {registry.find(ctx.paths, name).name} ({harness}) angelegt"]
    if not load_config(ctx.paths).has("moshi"):
        notes.append("Nur lokal: hier ist kein moshi eingerichtet, es gibt also keine moshi-Identität")
    if not found.installed(ctx.paths):
        notes.append(f"Hinweis: {found.binary} ist hier nicht installiert")
    return notes + skill_note(ctx, skill)


def wants_servers(ctx: AgentContext, harness: str) -> bool:
    config = load_config(ctx.paths)
    return HARNESSES[harness].native_mesh and (config.has("moshi") or config.has("ocellio"))


def _ask_name(ctx: AgentContext, io: WizardIO) -> str:
    existing = {a.name.lower() for a in load_agents(ctx.paths)}
    for _ in range(3):
        answer = io.ask("Name des Agents (z. B. recherche): ").strip()
        try:
            name = validate_name(answer)
        except ValueError as exc:
            io.say(f"   {exc}")
            continue
        if name.lower() in existing:
            io.say(f"   Es gibt schon einen Agent '{name}'")
            continue
        return name
    raise ValueError("Kein gültiger Name eingegeben, abgebrochen")


def _ask_harness(ctx: AgentContext, io: WizardIO) -> str:
    choices = harness_choices(ctx.paths)
    if not choices:
        raise ValueError("Keine Harness verfügbar: keine installiert, oder agy hat schon seinen einen Agent")
    for i, name in enumerate(choices, 1):
        io.say(f"   {i}. {name}")
    for line in unavailable(ctx.paths):
        io.say(f"      nicht angeboten: {line}")
    for _ in range(3):
        answer = io.ask(f"Harness (1–{len(choices)} oder Name): ").strip().lower()
        if answer.isdigit() and 1 <= int(answer) <= len(choices):
            return choices[int(answer) - 1]
        if answer in choices:
            return answer
        io.say("   Bitte eine der angezeigten Harnesses wählen")
    raise ValueError("Keine Harness gewählt, abgebrochen")


def run(ctx: AgentContext, io: WizardIO = WizardIO(), name: Optional[str] = None, harness: Optional[str] = None,
        role: Optional[str] = None, connect: Optional[Callable[[str], List[str]]] = None, skill: str = "") -> List[str]:
    """The guided path: asks for whatever is missing, creates the agent, offers to connect it."""
    name = validate_name(name) if name else _ask_name(ctx, io)
    if harness:
        check_harness(ctx, harness)
    else:
        harness = _ask_harness(ctx, io)
    if role is None:
        role = io.ask("Rolle in wenigen Worten (leer lassen geht): ").strip()
    for note in create(ctx, name, harness, role, skill):
        io.say(f"• {note}")  # at once: connecting may ask for more
    if not wants_servers(ctx, harness):
        return ["Starten: im „+“-Menü oder mit moshi-cockpit run " + name]
    config = load_config(ctx.paths)
    servers = " und ".join(s for s in ("moshi", "ocellio") if config.has(s))
    moshi_name = f" (moshi-Name {identity(name, config.person)})" if config.has("moshi") else ""
    answer = io.ask(f"Jetzt bei {servers} anmelden{moshi_name}? [J/n] ").strip().lower()
    if answer in ("", "j", "ja", "y", "yes") and connect is not None:
        return connect(name)
    return [f"Später anmelden: moshi-cockpit bot add {name}"]
