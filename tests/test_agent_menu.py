"""Agents in herdr's menus and start paths: one "+" entry per agent, the cockpit's own menu, the shell wrapper
and the standard entries for Vibe and Claude Code."""

import io
import json
import unittest
from contextlib import redirect_stdout
from unittest import mock

from cockpit import launch
from tests.helpers import OWN, configure, context, start


class AgentMenuTest(unittest.TestCase):  # one entry per agent in herdr's "+" menu
    def ctx(self, agents, servers=None):
        ctx, _ = context(self)
        ctx.paths.herdr_app_home.mkdir(parents=True, exist_ok=True)
        configure(ctx.paths, servers=servers, agents=agents)
        return ctx, ctx.paths.herdr_app_home / "programs.json"

    def sync(self, ctx):
        from cockpit import defaults
        return defaults.sync_agent_menu(ctx.paths, ctx.backup)

    def test_every_agent_gets_a_start_entry_and_grok_says_it_has_no_own_moshi(self):
        ctx, target = self.ctx([{"name": "claude-code", "harness": "claude"}, {"name": "g1", "harness": "grok"},
                                {"name": "v1", "harness": "vibe"}])
        self.sync(ctx)
        programs = json.loads(target.read_text())["programs"]
        entry = programs["moshi-cockpit-claude-code"]
        self.assertEqual(entry["name"], "claude-code")
        self.assertEqual(entry["run"], [str(ctx.paths.local_bin / "moshi-cockpit"), "run", "claude-code"])
        self.assertEqual(entry["icon"], "icons/claude.svg")
        self.assertEqual(programs["moshi-cockpit-g1"]["name"], "g1 (kein eigenes moshi)")
        self.assertEqual((programs["moshi-cockpit-v1"]["icon"], programs["moshi-cockpit-v1"]["tint"]), ("mesh-icons/vibe.svg", True))

    def test_without_moshi_nothing_is_marked_and_no_token_is_ever_written(self):
        ctx, target = self.ctx([{"name": "g1", "harness": "grok"}], servers={})
        self.sync(ctx)
        self.assertEqual(json.loads(target.read_text())["programs"]["moshi-cockpit-g1"]["name"], "g1")
        self.assertNotIn(OWN, target.read_text())

    def test_removed_agents_go_and_other_entries_stay(self):
        ctx, target = self.ctx([{"name": "a1", "harness": "codex"}])
        existing = {"programs": {"moshi-cockpit-old": {"name": "old", "run": ["x"]}, "claude": {"run": ["my-claude"]}},
                    "shells": ["x"]}
        target.write_text(json.dumps(existing))
        self.sync(ctx)
        data = json.loads(target.read_text())
        self.assertNotIn("moshi-cockpit-old", data["programs"])
        self.assertEqual(data["programs"]["claude"], {"run": ["my-claude"]})
        self.assertEqual(data["shells"], ["x"])
        self.assertIn("moshi-cockpit-a1", data["programs"])

    def test_a_broken_file_is_left_alone(self):
        ctx, target = self.ctx([{"name": "a1", "harness": "codex"}])
        target.write_text("{kaputt")
        self.assertIn("nicht überschrieben", self.sync(ctx))
        self.assertEqual(target.read_text(), "{kaputt")

    def test_adding_and_removing_an_agent_updates_the_menu(self):
        from cockpit import removal, wizard
        ctx, target = self.ctx([], servers={})
        wizard.create(ctx, "neu", "codex")
        self.assertIn("moshi-cockpit-neu", json.loads(target.read_text())["programs"])
        removal.remove_agent(ctx, "neu")
        self.assertNotIn("moshi-cockpit-neu", json.loads(target.read_text())["programs"])


class OwnMenuTest(unittest.TestCase):  # the "+" menu of the cockpit layout, in sections
    TABS = 'x.child(button("tab-new", "+", () => openOverlay({ mod: "new-menu", kind: "popup", width: 250, height: 512 })))\n'

    def ctx(self, tabs=TABS):
        ctx, _ = context(self)
        mods = ctx.paths.herdr_mods / "tabs"
        mods.mkdir(parents=True)
        (mods / "mod.json").write_text('{"place": "none"}')
        if tabs is not None:
            (mods / "main.js").write_text(tabs)
        return ctx

    def test_the_tab_bar_copy_opens_our_menu_and_is_only_used_when_that_worked(self):
        from cockpit import defaults
        ctx = self.ctx()
        self.assertTrue(defaults.install_menu_tabs(ctx.paths, ctx.backup))
        copy = (ctx.paths.herdr_mods / "mesh-tabs" / "main.js").read_text()
        self.assertIn('mod: "mesh-new-menu"', copy)
        self.assertNotIn('mod: "new-menu"', copy)
        for changed in ("kein passender Aufruf", self.TABS * 2):
            ctx = self.ctx(changed)
            self.assertFalse(defaults.install_menu_tabs(ctx.paths, ctx.backup))
            self.assertFalse((ctx.paths.herdr_mods / "mesh-tabs" / "main.js").exists())

    def test_the_cockpit_layout_uses_the_copy_only_with_our_menu(self):
        from cockpit import defaults
        main = {"type": "row", "children": [{"id": "a", "children": [{"content": {"mod": "tabs"}}]}]}
        def mod(layout):
            return layout["children"][0]["children"][0]["content"]["mod"]
        self.assertEqual(mod(defaults.cockpit_layout(main, own_menu=True)), "mesh-tabs")
        self.assertEqual(mod(defaults.cockpit_layout(main)), "tabs")
        self.assertEqual(mod(main), "tabs")

    def test_doctor_notices_a_tab_bar_left_behind_by_a_herdr_update(self):
        from cockpit import defaults
        ctx = self.ctx()
        tabs = ctx.paths.herdr_mods / "tabs"
        (tabs / "main.js").write_text("a " + defaults.TABS_OPENS_MENU)
        self.assertEqual(len(defaults.menu_findings(ctx.paths)), 1)  # never generated
        self.assertTrue(defaults.install_menu_tabs(ctx.paths, ctx.backup))
        self.assertEqual(defaults.menu_findings(ctx.paths), [])
        (tabs / "main.js").write_text("b " + defaults.TABS_OPENS_MENU)
        self.assertIn("moshi-cockpit install", defaults.menu_findings(ctx.paths)[0])
        (tabs / "main.js").write_text("herdr opens its menu differently now")
        self.assertIn("Herdrs eigenes Menü", defaults.menu_findings(ctx.paths)[0])

    def test_doctor_warns_when_herdr_changes_the_original_menu(self):
        from cockpit import defaults
        ctx = self.ctx()
        target = ctx.paths.herdr_app_home / "installed.json"
        target.write_text(json.dumps({"new-menu": {"main.js": defaults.NEW_MENU_UPSTREAM}}))
        defaults.install_menu_tabs(ctx.paths, ctx.backup)
        self.assertEqual(defaults.menu_findings(ctx.paths), [])
        target.write_text(json.dumps({"new-menu": {"main.js": "anderer-stand"}}))
        self.assertEqual(len(defaults.menu_findings(ctx.paths)), 1)

    def test_programs_that_is_not_an_object_is_left_alone(self):
        from cockpit import defaults
        ctx = self.ctx()
        configure(ctx.paths, agents=[{"name": "a1", "harness": "codex"}])
        target = ctx.paths.herdr_app_home / "programs.json"
        target.write_text('{"programs": ["x"]}')
        self.assertIn("nicht überschrieben", defaults.sync_agent_menu(ctx.paths, ctx.backup))
        self.assertEqual(target.read_text(), '{"programs": ["x"]}')

    def test_agent_list_json_says_who_is_connected_without_reading_a_token(self):
        from cockpit import cli
        ctx, fake = context(self, {("moshi-c1", "c1"): OWN})
        configure(ctx.paths, agents=[{"name": "c1", "harness": "claude"}, {"name": "c2", "harness": "claude"},
                                     {"name": "g1", "harness": "grok"}])
        with mock.patch("cockpit.cli.Paths.from_env", return_value=ctx.paths), \
                mock.patch("cockpit.cli.default_store", return_value=ctx.keychain), redirect_stdout(io.StringIO()) as out:
            self.assertEqual(cli.main(["agent", "list", "--json"]), 0)
        data = json.loads(out.getvalue())
        state = {a["name"]: (a["native"], a["connected"]) for a in data["agents"]}
        self.assertEqual(state, {"c1": (True, True), "c2": (True, False), "g1": (False, False)})
        self.assertEqual({a["name"]: a["has_moshi_token"] for a in data["agents"]}, {"c1": True, "c2": False, "g1": False})
        self.assertNotIn(OWN, out.getvalue())
        self.assertFalse(any("-w" in args for args, _ in fake.calls))

    def test_a_moshi_token_counts_even_when_another_server_token_is_missing(self):
        from cockpit import cli
        ctx, _ = context(self, {("moshi-v1", "v1"): OWN})  # vibe wires ocellio too, that token is missing
        configure(ctx.paths, agents=[{"name": "v1", "harness": "vibe"}])
        with mock.patch("cockpit.cli.Paths.from_env", return_value=ctx.paths), \
                mock.patch("cockpit.cli.default_store", return_value=ctx.keychain), redirect_stdout(io.StringIO()) as out:
            self.assertEqual(cli.main(["agent", "list", "--json"]), 0)
        agent = json.loads(out.getvalue())["agents"][0]
        self.assertEqual((agent["connected"], agent["has_moshi_token"]), (False, True))


class ShellStartTest(unittest.TestCase):  # a wrapper in the person's shell (start menu) must still run
    def run_it(self, name, shell=None, terminal=True):
        ctx, _ = context(self, {("moshi-research", "research"): OWN, ("ocellio-research", "research"): OWN,
                                ("moshi-writer", "writer"): OWN})
        if shell is None:  # a real executable called zsh: CI images have none at /bin/zsh
            fake = ctx.paths.home / "zsh"
            fake.write_text("")
            fake.chmod(0o755)
            shell = self.shell = str(fake)
        configure(ctx.paths, agents=[{"name": "writer", "harness": "claude"}, {"name": "research", "harness": "codex"}])
        seen = {}
        with mock.patch.object(launch.shutil, "which", return_value="/usr/bin/true"):
            launch.run_agent(ctx.paths, ctx.keychain, name, ["Auftrag; rm x $(y)"], environ={"PATH": "/usr/bin", "SHELL": shell},
                             execve=lambda path, argv, env: seen.update(path=path, argv=argv, env=env),
                             on_terminal=lambda: terminal)
        return seen

    def test_claude_on_a_terminal_goes_through_the_shell_with_arguments_kept_positional(self):
        seen = self.run_it("writer")
        self.assertEqual(seen["path"], self.shell)
        self.assertEqual(seen["argv"][:4], [self.shell, "-ic", 'claude "$@"', "claude"])
        self.assertEqual(seen["argv"][-1], "Auftrag; rm x $(y)")  # one argument, never shell text
        self.assertIn("--settings", seen["argv"])  # the moshi deny rule still travels
        self.assertEqual(seen["env"]["MOSHI_COCKPIT_RUN"], "1")

    def test_direct_start_without_terminal_or_known_shell_and_for_other_harnesses(self):
        for kwargs in ({"name": "writer", "terminal": False}, {"name": "writer", "shell": "/usr/bin/fish"},
                       {"name": "writer", "shell": "zsh"}, {"name": "research"}):
            with self.subTest(**kwargs):
                seen = self.run_it(**kwargs)
                self.assertEqual(seen["path"], "/usr/bin/true")
                self.assertNotIn("MOSHI_COCKPIT_RUN", seen["env"])


class VibeStandardEntryTest(unittest.TestCase):  # herdr ships no Vibe: "Start a standard agent" gets one, with an icon
    def setup(self, existing=None, installed=True):
        from cockpit.harnesses import HARNESSES
        patcher = mock.patch.object(type(HARNESSES["vibe"]), "installed", return_value=installed)
        patcher.start()
        self.addCleanup(patcher.stop)
        ctx, _ = context(self)
        ctx.paths.herdr_app_home.mkdir(parents=True, exist_ok=True)
        target = ctx.paths.herdr_app_home / "programs.json"
        if existing is not None:
            target.write_text(existing)
        return ctx, target

    def test_adds_the_entry_once_and_keeps_the_others(self):
        from cockpit import defaults
        ctx, target = self.setup(json.dumps({"programs": {"claude": {"name": "Mein Claude"}}}))
        self.assertIn("ergänzt", defaults.ensure_vibe_entry(ctx.paths, ctx.backup))
        self.assertIn("unverändert", defaults.ensure_vibe_entry(ctx.paths, ctx.backup))
        programs = json.loads(target.read_text())["programs"]
        self.assertEqual(programs["claude"], {"name": "Mein Claude"})
        self.assertEqual((programs["vibe"]["run"], programs["vibe"]["icon"]), (["vibe", "--agent", "ask"], "mesh-icons/vibe.svg"))

    def test_an_entry_from_an_earlier_install_is_updated(self):
        from cockpit import defaults
        ctx, target = self.setup(json.dumps({"programs": {"vibe": {"icon": defaults.VIBE_ICON, "run": ["vibe"]}}}))
        self.assertIn("ergänzt", defaults.ensure_vibe_entry(ctx.paths, ctx.backup))
        self.assertEqual(json.loads(target.read_text())["programs"]["vibe"], defaults.VIBE_STANDARD)

    def test_nothing_is_added_without_vibe(self):
        from cockpit import defaults
        ctx, target = self.setup(installed=False)
        self.assertIn("nicht installiert", defaults.ensure_vibe_entry(ctx.paths, ctx.backup))
        self.assertFalse(target.exists())

    def test_the_persons_entry_and_a_broken_file_stay(self):
        from cockpit import defaults
        mine = {"programs": {"vibe": {"run": ["my-vibe"]}}}
        ctx, target = self.setup(json.dumps(mine))
        self.assertIn("bleibt", defaults.ensure_vibe_entry(ctx.paths, ctx.backup))
        self.assertEqual(json.loads(target.read_text()), mine)
        target.write_text("{ kaputt")
        self.assertIn("übersprungen", defaults.ensure_vibe_entry(ctx.paths, ctx.backup))
        self.assertEqual(target.read_text(), "{ kaputt")

    def test_the_icon_lands_where_the_entry_points(self):
        from cockpit import defaults
        ctx, _ = self.setup()
        self.assertEqual(defaults.install_icons(ctx.paths, ctx.backup), ["vibe.svg"])
        self.assertIn("<svg", (ctx.paths.herdr_app_home / defaults.VIBE_STANDARD["icon"]).read_text())


class ClaudeMenuEntryTest(unittest.TestCase):  # herdr's own "Claude Code" button goes through the shell too
    def setup(self, existing=None, shell_name="zsh"):
        ctx, _ = context(self)
        ctx.paths.herdr_app_home.mkdir(parents=True, exist_ok=True)
        shell = ctx.paths.home / shell_name
        shell.write_text("")
        shell.chmod(0o755)
        target = ctx.paths.herdr_app_home / "programs.json"
        if existing is not None:
            target.write_text(existing)
        return ctx, str(shell), target

    def apply(self, ctx, shell):
        from cockpit import defaults
        return defaults.ensure_claude_launcher(ctx.paths, ctx.backup, {"SHELL": shell})

    def test_sets_the_run_field_and_keeps_everything_else(self):
        existing = json.dumps({"programs": {"lazygit": {"name": "Git"}, "claude": {"name": "Mein Claude"}}, "shells": ["x"]})
        ctx, shell, target = self.setup(existing)
        self.apply(ctx, shell)
        data = json.loads(target.read_text())
        self.assertEqual(data["programs"]["claude"], {"name": "Mein Claude", "run": [shell, "-ic", 'claude "$@"', "claude"]})
        self.assertEqual((data["programs"]["lazygit"], data["shells"]), ({"name": "Git"}, ["x"]))
        self.assertIn("unverändert", self.apply(ctx, shell))  # a second install changes nothing

    def test_never_replaces_a_persons_own_command_or_a_broken_file(self):
        own = json.dumps({"programs": {"claude": {"run": ["my-claude"]}}})
        ctx, shell, target = self.setup(own)
        self.assertIn("bleibt", self.apply(ctx, shell))
        self.assertEqual(target.read_text(), own)
        ctx, shell, target = self.setup("{kaputt")
        self.assertIn("nicht überschrieben", self.apply(ctx, shell))
        self.assertEqual(target.read_text(), "{kaputt")

    def test_other_shells_change_nothing(self):
        ctx, shell, target = self.setup(shell_name="fish")
        self.assertIn("unverändert", self.apply(ctx, shell))
        self.assertFalse(target.exists())


if __name__ == "__main__":
    unittest.main()
