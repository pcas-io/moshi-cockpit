"""What agents on this machine need to work with each other, put in place by `install`: the `agent-mesh` skill
and one herdr-core template per agent.

The skill goes to `~/.agents/skills/agent-mesh/` (read by agent CLIs that look there) with a link from
`~/.claude/skills/` when Claude Code is set up. A template is named after its agent and starts it through
`moshi-cockpit run`, so `hc run --bg --name job <agent> "Moshi <id> — …"` starts a helper with its own identity,
label and doorbell. The skill goes through the backup store. Templates do not: uninstall would delete a file the
person has since taken over. A template counts as ours only while it still carries our mark and starts our
`moshi-cockpit run`; only such a file is ever overwritten or removed.
"""

from __future__ import annotations

import os
import shlex
import tempfile
from pathlib import Path
from typing import List, Optional

from cockpit.backup import BackupStore
from cockpit.paths import SHARE, Paths

SKILL = "agent-mesh"
SKILLS = ("agent-mesh", "orchestrate")  # orchestrate: for an agent that hands work to others (the Conductor)
TEMPLATE_MARK = "moshi-cockpit agent with its own moshi identity"
# Arguments a template adds for a harness (hc runs Codex without its daemon).
TEMPLATE_ARGS = {"codex": ["--no-daemon"]}


def skill_dir(paths: Paths, skill: str = SKILL) -> Path:
    return paths.home / ".agents" / "skills" / skill


def templates_dir(paths: Paths) -> Path:
    return paths.home / ".config" / "herdr-core" / "templates"


def install_skill(paths: Paths, backup: BackupStore, skill: str = SKILL) -> str:
    text = (SHARE / "skills" / skill / "SKILL.md").read_text()
    changed = backup.write_text(skill_dir(paths, skill) / "SKILL.md", text)
    claude_skills = paths.home / ".claude" / "skills"
    if (paths.home / ".claude").is_dir():
        link = claude_skills / skill
        if link.exists() and not link.is_symlink():
            return f"Skill {skill}: ~/.claude/skills/{skill} is your own folder, not linked"
        changed = backup.symlink(skill_dir(paths, skill), link) or changed
    return f"Skill {skill}: " + ("installed" if changed else "unchanged")


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


def ours(paths: Paths, path: Path) -> bool:
    """Our mark in the description and our `moshi-cockpit run` as the command, both in the frontmatter."""
    try:
        head = path.read_text(encoding="utf-8")[:1000]
    except (OSError, UnicodeDecodeError):
        return False
    front = head.split("\n---", 1)[0] if head.startswith("---\n") else ""
    command = f"command: {shlex.quote(str(paths.local_bin / 'moshi-cockpit'))} run "
    return f"{TEMPLATE_MARK}." in front and f"\n{command}" in front


def _write(target: Path, text: str) -> bool:
    if target.is_file() and target.read_text(encoding="utf-8") == text:
        return False
    target.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(target.parent), prefix=f".{target.name}.", suffix=".moshi-cockpit")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(text)
        os.chmod(tmp, 0o644)
        os.replace(tmp, target)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)
    return True


def remove_templates(paths: Paths) -> str:
    """uninstall: every template that is still ours; the person's own stay."""
    folder = templates_dir(paths)
    gone = [p for p in sorted(folder.glob("*.md")) if ours(paths, p)] if folder.is_dir() else []
    for path in gone:
        path.unlink()
    return f"hc templates removed: {', '.join(p.stem for p in gone)}" if gone else ""


def remove_template(paths: Paths, name: str) -> None:
    """The template of an agent that left this machine; only one moshi-cockpit wrote."""
    target = templates_dir(paths) / f"{name}.md"
    if target.is_file() and ours(paths, target):
        target.unlink()


def sync_templates(paths: Paths, backup: BackupStore, agents: Optional[list] = None) -> str:
    """One template per agent. A file of the same name that moshi-cockpit did not write stays as it is."""
    from cockpit.registry import load_agents
    agents = agents if agents is not None else load_agents(paths, enforce=False)
    written: List[str] = []
    kept: List[str] = []
    for agent in agents:
        target = templates_dir(paths) / f"{agent.name}.md"
        if target.exists() and not ours(paths, target):
            kept.append(agent.name)
            continue
        if _write(target, template_text(paths, agent.name, agent.harness)):
            written.append(agent.name)
    note = "hc templates: " + (", ".join(written) if written else "unchanged")
    if kept:
        note += f" (your own template kept for {', '.join(kept)})"
    return note
