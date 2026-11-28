"""What agents on this machine need to work with each other, put in place by `install`: the `agent-mesh` skill
and one herdr-core template per agent.

The skill goes to `~/.agents/skills/agent-mesh/` (read by agent CLIs that look there) with a link from
`~/.claude/skills/` when Claude Code is set up. A template is named after its agent and starts it through
`moshi-cockpit run`, so `hc run --bg --name job <agent> "Moshi <id> — …"` starts a helper with its own identity,
label and doorbell. Every write goes through the backup store; a template file that moshi-cockpit did not write
is never overwritten.
"""

from __future__ import annotations

import shlex
from pathlib import Path
from typing import List, Optional

from cockpit.backup import BackupStore
from cockpit.paths import SHARE, Paths

SKILL = "agent-mesh"
TEMPLATE_MARK = "moshi-cockpit agent with its own moshi identity"
# Arguments a template adds for a harness (hc runs Codex without its daemon).
TEMPLATE_ARGS = {"codex": ["--no-daemon"]}


def skill_dir(paths: Paths) -> Path:
    return paths.home / ".agents" / "skills" / SKILL


def templates_dir(paths: Paths) -> Path:
    return paths.home / ".config" / "herdr-core" / "templates"


def install_skill(paths: Paths, backup: BackupStore) -> str:
    text = (SHARE / "skills" / SKILL / "SKILL.md").read_text()
    changed = backup.write_text(skill_dir(paths) / "SKILL.md", text)
    claude_skills = paths.home / ".claude" / "skills"
    if (paths.home / ".claude").is_dir():
        link = claude_skills / SKILL
        if link.exists() and not link.is_symlink():
            return f"Skill {SKILL}: ~/.claude/skills/{SKILL} is your own folder, not linked"
        changed = backup.symlink(skill_dir(paths), link) or changed
    return f"Skill {SKILL}: " + ("installed" if changed else "unchanged")


def template_text(paths: Paths, name: str, harness: str) -> str:
    command = " ".join(shlex.quote(part) for part in
                       [str(paths.local_bin / "moshi-cockpit"), "run", name, *TEMPLATE_ARGS.get(harness, [])])
    return "\n".join([
        "---",
        f"description: {name} ({harness}), {TEMPLATE_MARK}. The task line is a moshi doorbell.",
        f"command: {command}",
        "---",
        f"Read and follow the {SKILL} skill (~/.agents/skills/{SKILL}/SKILL.md) before you act.",
        "The line below is a doorbell: load that moshi message with mesh_get. It is your task.",
    ]) + "\n"


def ours(path: Path) -> bool:
    try:
        return TEMPLATE_MARK in path.read_text()[:500]
    except (OSError, UnicodeDecodeError):
        return False


def remove_template(paths: Paths, name: str) -> None:
    """The template of an agent that left this machine; only one moshi-cockpit wrote."""
    target = templates_dir(paths) / f"{name}.md"
    if target.is_file() and ours(target):
        target.unlink()


def sync_templates(paths: Paths, backup: BackupStore, agents: Optional[list] = None) -> str:
    """One template per agent. A file of the same name that moshi-cockpit did not write stays as it is."""
    from cockpit.registry import load_agents
    agents = agents if agents is not None else load_agents(paths, enforce=False)
    written: List[str] = []
    kept: List[str] = []
    for agent in agents:
        target = templates_dir(paths) / f"{agent.name}.md"
        if target.exists() and not ours(target):
            kept.append(agent.name)
            continue
        if backup.write_text(target, template_text(paths, agent.name, agent.harness)):
            written.append(agent.name)
    note = "hc templates: " + (", ".join(written) if written else "unchanged")
    if kept:
        note += f" (your own template kept for {', '.join(kept)})"
    return note
