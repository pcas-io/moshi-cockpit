import io
import json
import os
import unittest
from contextlib import redirect_stderr, redirect_stdout
from unittest import mock
from urllib.parse import quote

from cockpit import cli, sessions
from tests.helpers import configure, temp_paths

W = "/Users/anna/Workspace"


def write(path, text):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)
    return path


def jsonl(*entries):
    return "\n".join(json.dumps(e) for e in entries) + "\n"


class SessionsTest(unittest.TestCase):
    def setUp(self):
        self.paths = temp_paths(self)
        home = self.paths.home
        # claude: title from aiTitle, folder by its project dir
        write(home / ".claude/projects" / sessions.claude_project_dir(W) / "11111111-aaaa-bbbb-cccc-000000000001.jsonl",
              jsonl({"type": "user", "cwd": W, "message": {"content": "first question"}},
                    {"type": "ai-title", "aiTitle": "Build the panel\x1b[2J"}))
        write(home / ".claude/projects/-tmp/22222222-aaaa-bbbb-cccc-000000000002.jsonl",
              jsonl({"type": "user", "cwd": "/tmp", "message": {"content": [{"type": "text", "text": "elsewhere"}]}}))
        # codex: injected context is skipped, the panel prefix is replaced by the task
        write(home / ".codex/sessions/2026/11/07/rollout-1.jsonl",
              jsonl({"type": "session_meta", "payload": {"id": "33333333-aaaa-bbbb-cccc-000000000003", "cwd": W}},
                    {"type": "response_item", "payload": {"type": "message", "role": "user",
                                                           "content": [{"type": "input_text", "text": "<environment_context>"}]}},
                    {"type": "response_item", "payload": {"type": "message", "role": "user", "content": [
                        {"type": "input_text", "text": "Melde dich gemäß Präsenzregel in moshi an (x). Dann dein Auftrag von Anna:\n\nFix the tests"}]}}))
        # grok: urlencoded folder, summary on top level
        write(home / ".grok/sessions" / quote(W, safe="") / "44444444-aaaa-bbbb-cccc-000000000004/summary.json",
              json.dumps({"info": {"id": "44444444-aaaa-bbbb-cccc-000000000004"}, "session_summary": "Adjust Herdr",
                          "updated_at": "2026-11-07T16:13:49Z"}))
        # vibe: no title → first user turn; archived sessions are left out
        vibe = self.paths.standard_vibe_home / "logs/session/unified"
        write(vibe / "55555555-aaaa-bbbb-cccc-000000000005/meta.json",
              json.dumps({"session_id": "55555555-aaaa-bbbb-cccc-000000000005", "title": None,
                          "bumped_at": "2026-11-07T14:39:00Z", "environment": {"working_directory": W}}))
        write(vibe / "55555555-aaaa-bbbb-cccc-000000000005/chunks/a.json",
              json.dumps([{"message": {"role": "user", "content": [{"type": "text", "text": "Answer only with OK"}]},
                           "turn_index": 0}]))
        write(vibe / "d0000000-0000-0000-0000-000000000000/meta.json",
              json.dumps({"session_id": "d0000000-0000-0000-0000-000000000000", "archived_at": "2026-11-01",
                          "environment": {"working_directory": W}}))
        # agy: workspace URI
        write(home / ".gemini/antigravity-cli/cache/conversation_metadata.json",
              json.dumps({"conversations": {"66666666-aaaa-bbbb-cccc-000000000006": {"summary": {
                  "ID": "66666666-aaaa-bbbb-cccc-000000000006", "Title": "Moshi Presence", "UpdatedAt": "2026-11-07T15:17:00Z",
                  "WorkspaceURIs": [f"file://{W}"]}}}}))

    def test_every_agent_lists_its_session_in_the_folder_with_resume_args(self):
        expected = {"claude": ("Build the panel", ["--resume"]), "codex": ("Fix the tests", ["resume"]),
                    "grok": ("Adjust Herdr", ["--resume"]), "vibe": ("Answer only with OK", ["--resume"]),
                    "agy": ("Moshi Presence", ["--conversation"])}
        for agent, (title, flag) in expected.items():
            with self.subTest(agent=agent):
                found = sessions.list_sessions(agent, self.paths, W)
                self.assertEqual(len(found), 1, found)
                self.assertTrue(found[0]["title"].startswith(title), found[0])
                self.assertNotIn("\x1b", found[0]["title"])
                self.assertEqual(found[0]["resume_args"], flag + [found[0]["id"]])
                self.assertEqual(found[0]["cwd"], W)

    def test_all_folders_and_folder_filter(self):
        self.assertEqual(len(sessions.list_sessions("claude", self.paths, None)), 2)
        self.assertEqual(len(sessions.list_sessions("claude", self.paths, "/elsewhere")), 0)

    def test_ids_that_could_be_options_or_paths_are_dropped(self):
        write(self.paths.standard_vibe_home / "logs/session/unified/x/meta.json",
              json.dumps({"session_id": "--yolo", "environment": {"working_directory": W}}))
        write(self.paths.standard_vibe_home / "logs/session/unified/y/meta.json",
              json.dumps({"session_id": "../../etc/passwd", "environment": {"working_directory": W}}))
        ids = [s["id"] for s in sessions.list_sessions("vibe", self.paths, None)]
        self.assertEqual(ids, ["55555555-aaaa-bbbb-cccc-000000000005"])

    def test_the_old_panel_prefix_is_recognised_in_both_wordings(self):
        for prefix, task in (("Melde dich gemäß Präsenzregel in moshi an (x). ", "Dann dein Auftrag von Sam:"),
                             ("Sign in to moshi as the presence rule says (x). ", "Then your task from Sam:")):
            with self.subTest(prefix=prefix):
                self.assertEqual(sessions._readable(f"{prefix}{task}\n\nFix the tests"), "Fix the tests")
                self.assertEqual(sessions._readable(prefix.strip()), "(signed in, no task)")
        self.assertEqual(sessions._readable("Fix the tests"), "Fix the tests")

    def test_long_titles_are_cut(self):
        title = sessions.list_sessions("claude", self.paths, W)[0]["title"]
        self.assertLessEqual(len(title), sessions.MAX_TITLE + 1)

    def test_unknown_agent_and_missing_stores(self):
        with self.assertRaises(ValueError):
            sessions.list_sessions("emacs", self.paths)
        empty = temp_paths(self)
        for agent in sessions.AGENTS:
            self.assertEqual(sessions.list_sessions(agent, empty, None), [])


class RenameEveryHarnessTest(unittest.TestCase):
    """A name given with /rename wins over what the CLI generated, whichever CLI it is."""
    ID = "44444444-aaaa-bbbb-cccc-000000000004"

    def test_grok(self):
        paths = temp_paths(self)
        write(paths.home / ".grok/sessions" / quote("/tmp/w", safe="") / self.ID / "summary.json",
              json.dumps({"info": {"id": self.ID}, "session_summary": "automatische Zusammenfassung",
                          "generated_title": "mein Name", "title_is_manual": True, "updated_at": "2026-11-08T10:00:00Z"}))
        self.assertEqual(sessions.read_grok(paths, None, 5)[0]["title"], "mein Name")

    def test_claude(self):
        paths = temp_paths(self)
        write(paths.home / ".claude/projects/-tmp-w" / (self.ID + ".jsonl"),
              jsonl({"type": "user", "cwd": "/tmp/w", "message": {"content": [{"type": "text", "text": "erster Prompt"}]}},
                    {"type": "custom-title", "customTitle": "mein Name"}))
        self.assertEqual(sessions.read_claude(paths, None, 5)[0]["title"], "mein Name")

    def test_vibe(self):
        paths = temp_paths(self)
        write(paths.standard_vibe_home / "logs/session/unified" / self.ID / "meta.json",
              json.dumps({"session_id": self.ID, "title": "mein Name", "title_source": "user",
                          "bumped_at": "2026-11-08T10:00:00Z", "environment": {"working_directory": "/tmp/w"}}))
        self.assertEqual(sessions.read_vibe(paths, None, 5)[0]["title"], "mein Name")

    def test_agy(self):
        paths = temp_paths(self)
        write(paths.home / ".gemini/antigravity-cli/cache/conversation_metadata.json",
              json.dumps({"conversations": {self.ID: {"summary": {"ID": self.ID, "Title": "mein Name", "Preview": "erster Prompt",
                                                                  "UpdatedAt": "2026-11-08T10:00:00Z",
                                                                  "WorkspaceURIs": ["file:///tmp/w"]}}}}))
        self.assertEqual(sessions.read_agy(paths, None, 5)[0]["title"], "mein Name")


class CodexRenameTest(unittest.TestCase):
    ID = "33333333-aaaa-bbbb-cccc-000000000003"

    def rollout(self, paths, *entries):
        write(paths.home / ".codex/sessions/2026/11/08/rollout-1.jsonl",
              jsonl({"type": "session_meta", "payload": {"id": self.ID, "cwd": "/tmp/w"}}, *entries))

    def test_a_name_given_with_rename_replaces_the_first_prompt(self):
        paths = temp_paths(self)
        self.rollout(paths, {"type": "event_msg", "payload": {"type": "user_message", "message": "erster Prompt"}})
        self.assertEqual(sessions.read_codex(paths, None, 5)[0]["title"], "erster Prompt")
        write(paths.home / ".codex/session_index.jsonl",
              jsonl({"id": self.ID, "thread_name": "alter Name"}, {"id": "anderer", "thread_name": "fremd"},
                    {"id": self.ID, "thread_name": "ai-observatory"}))
        self.assertEqual(sessions.read_codex(paths, None, 5)[0]["title"], "ai-observatory")

    def test_the_injected_agents_block_is_not_a_title(self):
        paths = temp_paths(self)
        message = lambda text: {"type": "response_item", "payload": {"type": "message", "role": "user",
                                                                     "content": [{"type": "input_text", "text": text}]}}
        self.rollout(paths, message("# AGENTS.md instructions for /Users/x/w\n\nRules"), message("Build me the panel"))
        self.assertEqual(sessions.read_codex(paths, None, 5)[0]["title"], "Build me the panel")


class VibeHomeTest(unittest.TestCase):  # the standard entry and an own agent keep separate profiles
    def test_a_new_session_is_titled_from_its_journal(self):  # Vibe 3.1: no user turn in chunks any more
        paths = temp_paths(self)
        sid = "33333333-3333-4333-8333-333333333333"
        root = paths.standard_vibe_home / "logs/session/unified" / sid
        write(root / "meta.json", json.dumps({"session_id": sid, "title": None, "bumped_at": "2026-11-08T10:00:00+00:00",
                                               "environment": {"working_directory": W}}))
        write(root / "chunks/a.json", json.dumps([{"skills": []}]))
        write(root / "journal/0000000000000001.jsonl",
              json.dumps({"type": "x", "payload": {"params": {"entries": [{"role": "user", "content": "Build the test"}]}}}) + "\n")
        self.assertEqual(sessions.list_sessions("vibe", paths, None)[0]["title"], "Build the test")

    def test_a_journal_line_that_is_no_object_does_not_break_the_list(self):
        paths = temp_paths(self)
        sid = "44444444-4444-4444-8444-444444444444"
        root = paths.standard_vibe_home / "logs/session/unified" / sid
        write(root / "meta.json", json.dumps({"session_id": sid, "title": None, "bumped_at": "2026-11-08T10:00:00+00:00",
                                               "environment": {"working_directory": W}}))
        write(root / "journal/0000000000000001.jsonl", "[1, 2]\n" + json.dumps(
            {"payload": {"params": {"entries": [{"role": "user", "content": "Weiter"}]}}}) + "\n")
        self.assertEqual(sessions.list_sessions("vibe", paths, None)[0]["title"], "Weiter")

    def test_the_standard_list_never_shows_an_agents_sessions(self):
        paths = temp_paths(self)
        for home, sid in ((paths.standard_vibe_home, "11111111-1111-4111-8111-111111111111"),
                          (paths.legacy_vibe_home, "22222222-2222-4222-8222-222222222222")):
            write(home / "logs/session/unified" / sid / "meta.json",
                  json.dumps({"session_id": sid, "title": sid, "bumped_at": "2026-11-07T10:00:00+00:00",
                              "environment": {"working_directory": W}}))
        self.assertEqual([s["id"] for s in sessions.list_sessions("vibe", paths, None)], ["11111111-1111-4111-8111-111111111111"])
        self.assertEqual([s["id"] for s in sessions.list_sessions("vibe", paths, None, 8, paths.legacy_vibe_home)],
                         ["22222222-2222-4222-8222-222222222222"])


class SessionsCliTest(unittest.TestCase):  # standard agents of the "+" menu have a harness, not a name
    def run_cli(self, *args):
        paths = temp_paths(self)
        configure(paths, agents=[{"name": "a1", "harness": "grok"}])
        with mock.patch("cockpit.cli.Paths.from_env", return_value=paths), mock.patch("cockpit.cli.default_store"), \
                redirect_stdout(io.StringIO()) as out, redirect_stderr(io.StringIO()):
            code = cli.main(["sessions", *args])
        return code, out.getvalue()

    def test_by_harness_needs_no_registered_agent(self):
        code, out = self.run_cli("--harness=codex", "--json")
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(out), {"agent": "codex", "folder": "", "sessions": []})

    def test_exactly_one_of_agent_or_harness(self):
        self.assertEqual(self.run_cli()[0], 1)
        self.assertEqual(self.run_cli("a1", "--harness=grok")[0], 1)


if __name__ == "__main__":
    unittest.main()
