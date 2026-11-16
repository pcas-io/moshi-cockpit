"""The person's agents: a free name, a harness and a role each.

~/.config/moshi-cockpit/agents.json (0600):
    {"agents": [{"name": "research", "harness": "codex", "role": "Recherche", "home": null}, …]}

The name is the agent's identity everywhere: moshi name (`<person>-<name>` in the person model, see
person.py), keychain entries `<server>-<name>` / account `<name>`, the herdr tab "<name> · mesh".
Several agents may share a harness (two Codex agents, say), except agy, which reads one global config.
"""

from __future__ import annotations

import json
import re
from dataclasses import asdict, dataclass, field, replace
from pathlib import Path
from typing import Dict, List, Optional

from cockpit.backup import BackupStore
from cockpit.paths import Paths

# moshi's own rule for agent names (AGENT_NAME_RE), checked with fullmatch. The one source for agent and
# recipient names in moshi-cockpit: ASCII only, so no path parts ("../x"), spaces or Unicode look-alikes.
NAME_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,63}")
# Names with a meaning of their own: moshi's operator and fan-out, the human at the cockpit, and the
# registration token's binding key (tokens.py keeps non-agent bindings under these names).
RESERVED = {"admin", "broadcast", "cockpit", "register"}
MAX_ROLE = 120
_CONTROL = re.compile(r"[\x00-\x1f\x7f-\x9f]+")


@dataclass(frozen=True)
class Agent:
    name: str
    harness: str
    role: str = ""
    skill: str = ""  # name of an ocellio skill the agent loads at the start of a session
    home: Optional[str] = None  # a harness-specific folder, e.g. Vibe's VIBE_HOME; None = the default
    # server -> origin (scheme://host[:port]) its token was issued for. A token is only ever handed to
    # that origin; a changed server URL stops the agent until `bot add --rebind`.
    bound: Dict[str, str] = field(default_factory=dict)

    def to_json(self) -> dict:
        return {k: v for k, v in asdict(self).items() if v not in (None, "", {})}


def validate_name(name: str) -> str:
    name = name or ""  # not stripped: a name with surrounding whitespace is a different name, refuse it
    if not NAME_RE.fullmatch(name):
        raise ValueError("Ungültiger Agent-Name (Buchstaben, Ziffern, _ und -, am Anfang Buchstabe oder Ziffer, "
                         "höchstens 64 Zeichen)")
    if name.lower() in RESERVED:
        raise ValueError(f"'{name}' ist reserviert ({', '.join(sorted(RESERVED))})")
    return name


def _validate(raw: object, harnesses: Optional[List[str]] = None) -> Agent:
    if not isinstance(raw, dict):
        raise ValueError("Eintrag ist kein Objekt")
    name = validate_name(str(raw.get("name") or ""))
    harness = str(raw.get("harness") or "")
    if harnesses is not None and harness not in harnesses:
        raise ValueError(f"Agent '{name}': unbekannte Harness '{harness[:40]}'")
    # One line, control characters (NUL, ESC, …) out, bounded; the panel does the same (rules.js roleText).
    role = " ".join(_CONTROL.sub(" ", str(raw.get("role") or "")).split())[:MAX_ROLE]
    skill = str(raw.get("skill") or "")
    if skill and not NAME_RE.fullmatch(skill):
        raise ValueError(f"Agent '{name}': ungültiger Skill-Name (Buchstaben, Ziffern, _ und -)")
    home = raw.get("home")
    if home is not None and (not isinstance(home, str) or not Path(home).is_absolute()):
        raise ValueError(f"Agent '{name}': 'home' muss ein absoluter Pfad sein")
    bound = raw.get("bound") or {}
    if not isinstance(bound, dict) or not all(isinstance(k, str) and isinstance(v, str) for k, v in bound.items()):
        raise ValueError(f"Agent '{name}': 'bound' hat ein unerwartetes Format")
    return Agent(name=name, harness=harness, role=role, skill=skill, home=home, bound=dict(bound))


def single_instance_problems(agents: List[Agent]) -> List[str]:
    """agy reads one global config: two agy agents would post under one identity.
    One line per harness, with the way out."""
    from cockpit.harnesses import HARNESSES
    problems = []
    for harness in sorted({a.harness for a in agents}):
        same = [a.name for a in agents if a.harness == harness]
        if HARNESSES[harness].single_instance and len(same) > 1:
            problems.append(f"{harness} {HARNESSES[harness].single_reason}, es geht nur ein {harness}-Agent pro "
                            f"Rechner (gefunden: {', '.join(same)}). Aufräumen: moshi-cockpit agent remove {same[-1]}")
    return problems


def _check_single_instance(agents: List[Agent]) -> None:
    problems = single_instance_problems(agents)
    if problems:
        raise ValueError(problems[0])


def load_agents(paths: Paths, enforce: bool = True) -> List[Agent]:
    """Fail-closed: an agents file that exists but is broken is an error, not an empty list.

    enforce=False only for clean-up and display (agent remove/list, status, the panel, doctor): a list
    that breaks the one-agent rule must still be shown and repaired. Creating and starting enforce it."""
    from cockpit.harnesses import HARNESSES
    if not paths.agents_file.exists() and not paths.agents_file.is_symlink():
        return []
    from cockpit.config import read_private
    text = read_private(paths.agents_file, "Agent-Liste")
    try:
        data = json.loads(text)
    except ValueError:
        raise ValueError(f"{paths.agents_file} ist nicht lesbar oder kein gültiges JSON") from None
    items = data.get("agents") if isinstance(data, dict) else None
    if not isinstance(items, list):
        raise ValueError(f"{paths.agents_file} hat ein unerwartetes Format")
    agents, seen = [], set()
    for raw in items:
        try:
            agent = _validate(raw, list(HARNESSES))
        except ValueError as exc:
            raise ValueError(f"{paths.agents_file}: {exc}") from None
        if agent.name.lower() in seen:
            raise ValueError(f"{paths.agents_file}: Agent '{agent.name}' doppelt")
        seen.add(agent.name.lower())
        agents.append(agent)
    if enforce:
        try:
            _check_single_instance(agents)
        except ValueError as exc:
            raise ValueError(f"{paths.agents_file}: {exc}") from None
    return agents


def find(paths: Paths, name: str, enforce: bool = True) -> Agent:
    for agent in load_agents(paths, enforce=enforce):
        if agent.name.lower() == (name or "").lower():
            return agent
    raise ValueError(f"Unbekannter Agent '{(name or '')[:64]}' (moshi-cockpit agent list)")


def save_agents(paths: Paths, backup: BackupStore, agents: List[Agent]) -> bool:
    payload = {"agents": [a.to_json() for a in agents]}
    return backup.write_text(paths.agents_file, json.dumps(payload, indent=2, ensure_ascii=False) + "\n", mode=0o600)


def add(paths: Paths, backup: BackupStore, agent: Agent) -> List[Agent]:
    from cockpit.harnesses import HARNESSES
    agent = _validate(agent.to_json(), list(HARNESSES))
    agents = load_agents(paths)
    if any(a.name.lower() == agent.name.lower() for a in agents):
        raise ValueError(f"Es gibt schon einen Agent '{agent.name}'")
    agents.append(agent)
    _check_single_instance(agents)
    save_agents(paths, backup, agents)
    return agents


def update(paths: Paths, backup: BackupStore, name: str, role: Optional[str], skill: Optional[str]) -> Agent:
    """Changes role and skill; None leaves a field as it is. Checked like a new entry."""
    from cockpit.harnesses import HARNESSES
    agents = load_agents(paths, enforce=False)
    for i, agent in enumerate(agents):
        if agent.name.lower() != (name or "").lower():
            continue
        raw = agent.to_json()
        raw.update({k: v for k, v in (("role", role), ("skill", skill)) if v is not None})
        agents[i] = _validate(raw, list(HARNESSES))
        save_agents(paths, backup, agents)
        return agents[i]
    raise ValueError(f"Unbekannter Agent '{(name or '')[:64]}' (moshi-cockpit agent list)")


def bind(paths: Paths, backup: BackupStore, name: str, origins: Dict[str, str], rebind: bool = False) -> Agent:
    """Records which origin a server token belongs to. An existing, different binding is only replaced
    with rebind=True (an explicit human act: the token then goes to the new origin)."""
    agents = load_agents(paths)
    target = find(paths, name)
    bound = dict(target.bound)
    for server, value in origins.items():
        if bound.get(server) not in (None, value) and not rebind:
            raise ValueError(f"{target.name}: {server}-Token ist an {bound[server]} gebunden, die Config zeigt auf "
                             f"{value}. Absichtlich umgezogen? moshi-cockpit bot add {target.name} --rebind")
        bound[server] = value
    updated = replace(target, bound=bound)
    save_agents(paths, backup, [updated if a.name == target.name else a for a in agents])
    return updated


def remove(paths: Paths, backup: BackupStore, name: str) -> Agent:
    """Works on a list that breaks the one-agent rule too: removing is how it gets repaired."""
    agents = load_agents(paths, enforce=False)
    target = find(paths, name, enforce=False)
    save_agents(paths, backup, [a for a in agents if a.name != target.name])
    return target
