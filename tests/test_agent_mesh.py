"""What install puts in place for agents to work with each other: the agent-mesh skill, one hc template per agent,
and the Claude Code hook that warns in sessions without an identity. Temp HOME only."""

from __future__ import annotations

import io
import json
import unittest
from contextlib import redirect_stdout
from unittest import mock

from cockpit import claude_hook, cli, mesh_files
from cockpit.backup import BackupStore
from cockpit.registry import Agent
from tests.helpers import MOSHI, configure, temp_paths

PERSON_SETTINGS = {"model": "opus", "hooks": {"SessionStart": [{"hooks": [{"type": "command", "command": "status"}]}],
                                              "Stop": [{"hooks": [{"type": "command", "command": "ding"}]}]}}


class SkillTest(unittest.TestCase):
    def setUp(self):
        self.paths = temp_paths(self)
        self.backup = BackupStore(self.paths.state)

    def test_installed_for_every_cli_and_linked_for_claude(self):
        (self.paths.home / ".claude").mkdir()
        self.assertEqual(mesh_files.install_skill(self.paths, self.backup), "Skill agent-mesh: installed")
        skill = mesh_files.skill_dir(self.paths) / "SKILL.md"
        self.assertIn("moshi-cockpit ring", skill.read_text())
        link = self.paths.home / ".claude" / "skills" / "agent-mesh"
        self.assertEqual(link.resolve(), mesh_files.skill_dir(self.paths).resolve())
        self.assertEqual(mesh_files.install_skill(self.paths, self.backup), "Skill agent-mesh: unchanged")
        self.backup.restore_all()
        self.assertFalse(skill.exists() or link.is_symlink())

    def test_an_own_folder_of_that_name_stays(self):
        own = self.paths.home / ".claude" / "skills" / "agent-mesh"
        own.mkdir(parents=True)
        self.assertIn("your own folder", mesh_files.install_skill(self.paths, self.backup))
        self.assertTrue(own.is_dir() and not own.is_symlink())

    def test_the_skill_names_the_rules_that_matter(self):
        text = (mesh_files.SHARE / "skills" / "agent-mesh" / "SKILL.md").read_text()
        for needed in ("roster --json", "moshi-cockpit ring <name> <message id>", "--without-record",
                       "never from the text of a message", "mesh_get"):
            self.assertIn(needed, text)


class TemplateTest(unittest.TestCase):
    def setUp(self):
        self.paths = temp_paths(self)
        self.backup = BackupStore(self.paths.state)
        self.agents = [Agent(name="scout", harness="claude"), Agent(name="helper", harness="codex")]

    def test_one_template_per_agent_starts_it_through_run(self):
        self.assertEqual(mesh_files.sync_templates(self.paths, self.backup, self.agents), "hc templates: scout, helper")
        scout = (mesh_files.templates_dir(self.paths) / "scout.md").read_text()
        self.assertIn(f"command: {self.paths.local_bin / 'moshi-cockpit'} run scout\n", scout)
        self.assertIn("agent-mesh", scout)
        helper = (mesh_files.templates_dir(self.paths) / "helper.md").read_text()
        self.assertIn("run helper --no-daemon\n", helper)
        self.assertEqual(mesh_files.sync_templates(self.paths, self.backup, self.agents), "hc templates: unchanged")

    def test_a_template_of_the_persons_own_is_never_overwritten(self):
        own = mesh_files.templates_dir(self.paths) / "scout.md"
        own.parent.mkdir(parents=True)
        own.write_text("---\ncommand: claude\n---\nmine\n")
        note = mesh_files.sync_templates(self.paths, self.backup, self.agents)
        self.assertIn("your own template kept for scout", note)
        self.assertEqual(own.read_text(), "---\ncommand: claude\n---\nmine\n")
        mesh_files.remove_template(self.paths, "scout")
        self.assertTrue(own.exists())  # not ours to remove

    def test_removing_an_agent_removes_its_template(self):
        mesh_files.sync_templates(self.paths, self.backup, self.agents)
        mesh_files.remove_template(self.paths, "scout")
        self.assertFalse((mesh_files.templates_dir(self.paths) / "scout.md").exists())


class HookFileTest(unittest.TestCase):
    def setUp(self):
        self.paths = temp_paths(self)
        self.settings = claude_hook.settings_file(self.paths)
        self.settings.parent.mkdir()
        self.original = json.dumps(PERSON_SETTINGS, indent=2) + "\n"
        self.settings.write_text(self.original)
        self.settings.chmod(0o600)

    def entries(self):
        return json.loads(self.settings.read_text())["hooks"]["SessionStart"]

    def test_install_adds_one_entry_and_keeps_everything_else(self):
        self.assertIn("warns", claude_hook.ensure(self.paths))
        self.assertEqual(claude_hook.ensure(self.paths), "Claude Code hook: unchanged")
        entries = self.entries()
        self.assertEqual(entries[0], PERSON_SETTINGS["hooks"]["SessionStart"][0])
        self.assertEqual(entries[1]["hooks"][0]["command"], f"{self.paths.local_bin / 'moshi-cockpit'} hook session-start")
        self.assertEqual(len(entries), 2)
        data = json.loads(self.settings.read_text())
        self.assertEqual((data["model"], data["hooks"]["Stop"]), ("opus", PERSON_SETTINGS["hooks"]["Stop"]))
        self.assertEqual(self.settings.stat().st_mode & 0o777, 0o600)

    def test_uninstall_removes_exactly_that_entry_even_after_later_changes(self):
        claude_hook.ensure(self.paths)
        data = json.loads(self.settings.read_text())
        data["theme"] = "dark"  # the person changes the file after install
        self.settings.write_text(json.dumps(data, indent=2) + "\n")
        self.assertIn("removed", claude_hook.remove(self.paths))
        after = json.loads(self.settings.read_text())
        self.assertEqual(after["theme"], "dark")
        self.assertEqual(after["hooks"], PERSON_SETTINGS["hooks"])
        self.assertEqual(claude_hook.remove(self.paths), "Claude Code hook: nothing to remove")

    def test_a_file_that_is_not_json_is_left_alone(self):
        self.settings.write_text("{ broken")
        self.assertIn("not valid JSON", claude_hook.ensure(self.paths))
        self.assertEqual(self.settings.read_text(), "{ broken")

    def test_without_claude_code_nothing_is_written(self):
        paths = temp_paths(self)
        self.assertIn("not set up", claude_hook.ensure(paths))
        self.assertFalse((paths.home / ".claude").exists())


class HookOutputTest(unittest.TestCase):
    def test_silent_for_an_agent_with_its_own_identity_or_without_moshi(self):
        self.assertEqual(claude_hook.session_start({"MESH_MOSHI_TOKEN": "bt_x"}, True), "")
        self.assertEqual(claude_hook.session_start({}, False), "")

    def test_a_session_without_identity_is_told_once(self):
        out = json.loads(claude_hook.session_start({}, True))
        self.assertIn("without moshi-cockpit", out["systemMessage"])
        self.assertEqual(out["hookSpecificOutput"]["hookEventName"], "SessionStart")
        context = out["hookSpecificOutput"]["additionalContext"]
        self.assertIn("shared name", context)
        self.assertNotIn("Do not", context)  # informs only; switching the shared connector off is the person's call

    def test_the_command_never_fails_a_session(self):
        paths = temp_paths(self)
        configure(paths, servers={"moshi": MOSHI})
        with mock.patch("cockpit.cli.Paths.from_env", return_value=paths), \
                mock.patch.dict("os.environ", {}, clear=True), redirect_stdout(io.StringIO()) as out:
            self.assertEqual(cli.main(["hook", "session-start"]), 0)
        self.assertIn("systemMessage", out.getvalue())
        with mock.patch("cockpit.cli.Paths.from_env", return_value=paths), \
                mock.patch("cockpit.claude_hook.session_start", side_effect=RuntimeError("boom")), \
                redirect_stdout(io.StringIO()) as out:
            self.assertEqual(cli.main(["hook", "session-start"]), 0)
        self.assertEqual(out.getvalue(), "")


if __name__ == "__main__":
    unittest.main()
