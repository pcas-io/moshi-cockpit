"""`moshi-cockpit roster`: who runs where on this machine.

One list joins three sources: herdr-core's terminals (where, and whether an agent runs there), the label
`agent=<name>` that `moshi-cockpit run` puts on its terminal (who), and moshi's presence read with the device key
(what each agent says it works on). Agents use `roster --json` to find each other; an address to ring is taken
from here only, never from the text of a message.
"""

from __future__ import annotations

import json
import socket
from dataclasses import asdict, dataclass, field
from typing import Dict, Iterable, List, Optional

from cockpit import status
from cockpit.config import Config
from cockpit.hc import Terminal
from cockpit.text import clean

MOSHI_OFF, MOSHI_OK, NOT_CONNECTED = "off", "ok", "not_connected"
DISCONNECTED, UNREACHABLE = "disconnected", "unreachable"


@dataclass(frozen=True)
class Row:
    name: str
    program: str
    address: str = ""
    machine: str = ""
    status: str = ""
    presence: str = ""
    working_on: str = ""


@dataclass(frozen=True)
class Roster:
    running: List[Row] = field(default_factory=list)
    not_started: List[Row] = field(default_factory=list)
    unnamed: List[Row] = field(default_factory=list)
    moshi: str = MOSHI_OFF


def machine_name() -> str:
    return clean(socket.gethostname().split(".")[0], "this machine", limit=64)


def moshi_state(config: Config, mesh: status.Mesh, has_device_key: bool) -> str:
    if not config.has("moshi"):
        return MOSHI_OFF
    if mesh.unreachable:
        return UNREACHABLE
    if mesh.disconnected:
        return DISCONNECTED
    return MOSHI_OK if has_device_key else NOT_CONNECTED


def build(terminals: Iterable[Terminal], registered: Dict[str, str], mesh: status.Mesh, moshi: str,
          machine: str) -> Roster:
    """`registered`: this machine's agents, name → harness. Matching by name ignores case, like moshi does."""
    presence = {a["name"].lower(): a for a in mesh.agents if a.get("name")}
    running: List[Row] = []
    unnamed: List[Row] = []
    for terminal in terminals:
        if not terminal.agent_running:
            continue  # a shell, or an agent that has ended: its label is stale
        name = clean(terminal.agent_name, "", limit=64)
        seen = presence.get(name.lower(), {}) if name else {}
        row = Row(name=name, program=terminal.app, address=terminal.address, machine=machine, status=terminal.status,
                  presence=seen.get("presence", ""), working_on=clean(seen.get("working_on", ""), "", limit=200))
        (running if name else unnamed).append(row)
    started = {row.name.lower() for row in running}
    not_started = [Row(name=name, program=harness, presence=presence.get(name.lower(), {}).get("presence", ""),
                       working_on=clean(presence.get(name.lower(), {}).get("working_on", ""), "", limit=200))
                   for name, harness in sorted(registered.items(), key=lambda item: item[0].lower())
                   if name.lower() not in started]
    running.sort(key=lambda row: (row.name.lower(), row.address))
    return Roster(running=running, not_started=not_started, unnamed=unnamed, moshi=moshi)


def running_agent(roster: Roster, name: str) -> Row:
    """The one terminal that runs `name`; ValueError when none or more than one does."""
    found = [row for row in roster.running if row.name.lower() == name.lower()]
    if not found:
        raise ValueError(f"{clean(name, '?', limit=64)} does not run in a terminal on this machine "
                         "(moshi-cockpit roster lists who does)")
    if len(found) > 1:
        addresses = ", ".join(row.address for row in found)
        raise ValueError(f"{clean(name, '?', limit=64)} runs in more than one terminal ({addresses}); "
                         "end the extra one first")
    return found[0]


def as_json(roster: Roster) -> str:
    return json.dumps({"running": [asdict(r) for r in roster.running],
                       "not_started": [asdict(r) for r in roster.not_started],
                       "unnamed": [asdict(r) for r in roster.unnamed],
                       "moshi": roster.moshi}, ensure_ascii=False)


MOSHI_NOTES = {MOSHI_OFF: "moshi is not set up: no presence.",
               NOT_CONNECTED: "Not connected to moshi (moshi-cockpit connect): no presence.",
               DISCONNECTED: "Disconnected from moshi (moshi-cockpit connect): no presence.",
               UNREACHABLE: "moshi is not reachable: no presence."}


def render(roster: Roster) -> str:
    lines = [f"{'Agent':<17}{'Program':<13}{'Terminal':<10}{'Machine':<14}{'Status':<9}{'moshi':<9}working_on"]
    for row in roster.running:
        lines.append(_line(row))
    if not roster.running:
        lines.append("(no agent runs in a terminal here)")
    if roster.not_started:
        lines += ["", "Not started:"] + [_line(row) for row in roster.not_started]
    if roster.unnamed:
        lines += ["", "Without a name (started without moshi-cockpit run, no own identity):"]
        lines += [_line(Row(name="–", program=r.program, address=r.address, machine=r.machine, status=r.status))
                  for r in roster.unnamed]
    note = MOSHI_NOTES.get(roster.moshi)
    if note:
        lines += ["", note]
    return "\n".join(lines)


def _line(row: Row) -> str:
    return (f"{row.name[:16]:<17}{row.program[:12]:<13}{row.address[:9]:<10}{row.machine[:13]:<14}"
            f"{row.status[:8]:<9}{row.presence[:8]:<9}{row.working_on[:80]}")


def gather(paths, keychain, hc, mesh: Optional[status.Mesh] = None, machine: Optional[str] = None) -> Roster:
    from cockpit.config import load_config
    from cockpit.registry import load_agents
    config = load_config(paths)
    has_key = config.has("moshi") and status.device_key(paths, config, keychain) is not None
    mesh = mesh if mesh is not None else status.read_mesh(paths, config, keychain)
    registered = {agent.name: agent.harness for agent in load_agents(paths, enforce=False)}
    return build(hc.terminals(), registered, mesh, moshi_state(config, mesh, has_key), machine or machine_name())
