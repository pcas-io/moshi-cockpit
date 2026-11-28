"""`dispatch` (start or wake an agent for a moshi message, with model and effort) and the focus instead of a second
start. hc is a fake runner; moshi's mesh_get is a fake fetch. Nothing reaches a real hc or herdr-app."""

from __future__ import annotations

import io
import json
import unittest
from contextlib import redirect_stdout
from typing import Dict, List, Sequence, Tuple
from unittest import mock

from cockpit import cli, dispatch, launch, mesh_files
from cockpit.backup import BackupStore
from cockpit.harnesses.base import TOKEN_ENV
from cockpit.hc import AGENT_LABEL, Hc, HcError
from tests.helpers import MOSHI, configure, temp_paths
from tests.test_roster_ring import FakeHc, terminal

MSG = "msg_01M8GPA4ED6KZX8QEEJ4AN5R2V"
SPACE = "sp8910deddf93f5a8428864b414506d3fb"


class DispatchHc(FakeHc):
    """FakeHc that also answers `run`: it records the template as it was at that moment."""

    def __init__(self, terminals: List[Dict], templates) -> None:
        super().__init__(terminals)
        self.templates = templates
        self.seen_template = ""
        self.sent_first = True

    def __call__(self, argv: Sequence[str], timeout: int) -> Tuple[int, str, str]:
        if argv[1] == "run" and "run" not in self.fail:
            args = tuple(argv[1:-1])
            self.calls.append(args)
            self.seen_template = (self.templates / f"{args[-2]}.md").read_text()
            first = {"sent": True} if self.sent_first else {"sent": False, "reason": "not ready"}
            return 0, json.dumps({"address": "t9", "first_message": first}), ""
        return super().__call__(argv, timeout)

    def runs(self):
        return [c for c in self.calls if c[0] == "run"]


class HarnessArgsTest(unittest.TestCase):
    def test_model_as_it_is_and_effort_per_harness(self):
        cases = {"claude": ["--model", "m1", "--effort", "high"], "agy": ["--model", "m1", "--effort", "high"],
                 "codex": ["-m", "m1", "-c", 'model_reasoning_effort="high"'],
                 "grok": ["-m", "m1", "--reasoning-effort", "high"]}
        for harness, expected in cases.items():
            with self.subTest(harness=harness):
                self.assertEqual(dispatch.harness_args(harness, "m1", "high"), expected)
        self.assertEqual(dispatch.harness_args("vibe", "fast"), ["--model", "fast"])
        self.assertEqual(dispatch.harness_args("claude"), [])

    def test_refusals(self):
        with self.assertRaisesRegex(ValueError, "vibe has no effort"):
            dispatch.harness_args("vibe", effort="high")
        with self.assertRaisesRegex(ValueError, "--effort is one of"):
            dispatch.harness_args("claude", effort="ultra")
        with self.assertRaisesRegex(ValueError, "model name"):
            dispatch.harness_args("claude", model="x; rm -rf ~")


class DispatchTest(unittest.TestCase):
    def setUp(self):
        self.paths = temp_paths(self)
        configure(self.paths, servers={"moshi": MOSHI}, agents=[{"name": "Angy", "harness": "agy"},
                                                                {"name": "Conductor", "harness": "claude"}])
        self.templates = mesh_files.templates_dir(self.paths)
        self.fake = DispatchHc([terminal("t1", "claude-code", {AGENT_LABEL: "Conductor", "herdr.space": SPACE})],
                               self.templates)
        self.hc = Hc("/bin/hc", self.fake)
        self.env = {TOKEN_ENV["moshi"]: "bt_conductor", "COCKPIT_HC_ADDRESS": "t1@mac"}
        self.message = {"to": "Angy", "from": "Conductor", "type": "task_update"}

    def dispatch(self, name="Angy", message_id=MSG, env=None, **options):
        return dispatch.dispatch(self.paths, name, message_id, self.hc, environ=self.env if env is None else env,
                                 fetch=lambda url, token, mid: self.message, **options)

    def test_starts_a_visible_child_with_model_effort_and_the_doorbell(self):
        done = self.dispatch(model="gemini-4-carbon-high", effort="high", job="probe")
        self.assertEqual((done.address, done.action), ("t9", "started"))
        run = self.fake.runs()[0]
        template = run[-2]
        self.assertTrue(template.startswith(".dispatch-probe-"))
        self.assertEqual(run[:-2], ("run", "--bg", "--name", "probe", "--label", f"herdr.space={SPACE}",
                                    "--parent", "t1", "--outlive-parent"))
        self.assertEqual(run[-1], f"Moshi {MSG} — task_update from Conductor")
        self.assertIn("run Angy --model gemini-4-carbon-high --effort high\n", self.fake.seen_template)
        self.assertEqual(list(self.templates.glob(".dispatch-*")), [])  # short-lived

    def test_hidden_starts_without_a_tab(self):
        self.dispatch(hidden=True)
        self.assertNotIn("--label", self.fake.runs()[0])

    def test_wakes_a_running_agent_and_refuses_a_new_model_for_it(self):
        self.fake.terminals.append(terminal("t5", "antigravity", {AGENT_LABEL: "Angy"}))
        self.assertEqual(self.dispatch().action, "rang")
        self.assertEqual(self.fake.sent(), [("send", "t5", f"Moshi {MSG} — task_update from Conductor")])
        with self.assertRaisesRegex(ValueError, "runs already in t5"):
            self.dispatch(model="gemini-4-carbon-high")
        self.assertEqual(len(self.fake.sent()), 1)
        self.assertEqual(self.fake.runs(), [])

    def test_refusals_before_anything_starts(self):
        for options, words in ((dict(message_id="msg_x"), "msg_"), (dict(env={}), "no moshi identity"),
                               (dict(job="Bad Name"), "--name"), (dict(effort="ultra"), "--effort")):
            with self.subTest(options=options), self.assertRaisesRegex(ValueError, words):
                self.dispatch(**options)
        self.message = {"to": "Someone", "from": "Conductor", "type": "info"}
        with self.assertRaisesRegex(ValueError, "not addressed to Angy"):
            self.dispatch()
        with self.assertRaises(ValueError):
            self.dispatch(name="nobody")
        self.assertEqual(self.fake.runs(), [])

    def test_the_template_goes_also_when_hc_fails(self):
        self.fake.fail["run"] = "core not reachable"
        with self.assertRaisesRegex(HcError, "core not reachable"):
            self.dispatch()
        self.assertEqual(list(self.templates.glob(".dispatch-*")), [])

    def test_a_task_that_did_not_arrive_is_an_error_with_the_address(self):
        self.fake.sent_first = False
        with self.assertRaisesRegex(ValueError, "started in t9, but its task did not arrive"):
            self.dispatch()

    def test_never_writes_over_an_existing_file(self):
        target = self.templates / ".dispatch-x-aaaaaa.md"
        target.parent.mkdir(parents=True)
        target.write_text("mine")
        with self.assertRaises(FileExistsError):
            dispatch._write_new(target, "theirs")
        self.assertEqual(target.read_text(), "mine")

    def test_the_caller_terminal(self):
        self.assertEqual(dispatch.caller_terminal({"COCKPIT_HC_ADDRESS": "t4p@mac"}), "t4p")
        self.assertEqual(dispatch.caller_terminal({"HC_TERMINAL_ID": "t2"}), "t2")
        self.assertEqual(dispatch.caller_terminal({}), "")

    def test_cli_prints_json(self):
        with mock.patch("cockpit.cli.Paths.from_env", return_value=self.paths), \
                mock.patch("cockpit.cli._hc", return_value=self.hc), \
                mock.patch.object(dispatch, "dispatch", return_value=dispatch.Outcome("t9", "started")), \
                redirect_stdout(io.StringIO()) as out:
            self.assertEqual(cli.main(["dispatch", "Angy", MSG, "--json"]), 0)
        self.assertEqual(json.loads(out.getvalue()), {"address": "t9", "action": "started"})


class FocusTest(unittest.TestCase):
    def setUp(self):
        self.paths = temp_paths(self)
        configure(self.paths, servers={"moshi": MOSHI}, agents=[{"name": "Conductor", "harness": "claude"}])
        hc_bin = self.paths.local_bin / "hc"
        hc_bin.parent.mkdir(parents=True, exist_ok=True)
        hc_bin.write_text("#!/bin/sh\n")
        self.fake = FakeHc([terminal("t1", "claude-code", {AGENT_LABEL: "Conductor"}), terminal("t2", "")])

    def elsewhere(self, own):
        return launch.running_elsewhere(self.paths, "conductor", {"HC_TERMINAL_ID": own},
                                        hc_for=lambda binary: Hc(binary, self.fake))

    def test_finds_the_running_terminal_but_not_this_one(self):
        self.assertEqual(self.elsewhere("t2"), "t1")
        self.assertEqual(self.elsewhere("t1"), "")

    def test_without_hc_under_this_home_nothing_is_found(self):
        (self.paths.local_bin / "hc").unlink()
        self.assertEqual(launch.running_elsewhere(self.paths, "Conductor", {}), "")

    def test_run_shows_the_running_one_instead_of_starting_a_second(self):
        with mock.patch("cockpit.cli.Paths.from_env", return_value=self.paths), \
                mock.patch.object(launch, "running_elsewhere", return_value="t1"), \
                mock.patch.object(launch, "focus", return_value=True) as focus, \
                mock.patch.object(launch, "prepare") as prepare, redirect_stdout(io.StringIO()) as out:
            self.assertEqual(cli.main(["run", "Conductor"]), 0)
        focus.assert_called_once_with(self.paths, "t1")
        prepare.assert_not_called()
        self.assertIn("runs already in t1; its tab is in front now", out.getvalue())

    def test_take_over_starts_it_anyway(self):
        with mock.patch("cockpit.cli.Paths.from_env", return_value=self.paths), \
                mock.patch.object(launch, "running_elsewhere", return_value="t1") as elsewhere, \
                mock.patch.object(launch, "prepare", side_effect=ValueError("stop here")), \
                mock.patch("cockpit.cli._on_terminal", return_value=False):
            with self.assertRaisesRegex(ValueError, "stop here"):
                cli.cmd_run(cli.build_parser().parse_args(["run", "Conductor", "--take-over"]), self.paths)
        elsewhere.assert_not_called()

    def test_focus_uses_herdr_app_under_this_home_only(self):
        calls = []
        self.assertFalse(launch.focus(self.paths, "t1", runner=lambda argv, timeout: calls.append(argv) or (0, "", "")))
        app = self.paths.local_bin / "herdr-app"
        app.write_text("#!/bin/sh\n")
        self.assertTrue(launch.focus(self.paths, "t1", runner=lambda argv, timeout: calls.append(argv) or (0, "", "")))
        self.assertEqual(calls, [[str(app), "do", "agent.focus", "--terminal", "t1"]])


class SkillsTest(unittest.TestCase):
    def test_install_puts_both_skills_in_place(self):
        paths = temp_paths(self)
        backup = BackupStore(paths.state)
        for skill in mesh_files.SKILLS:
            mesh_files.install_skill(paths, backup, skill)
        orchestrate = (mesh_files.skill_dir(paths, "orchestrate") / "SKILL.md").read_text()
        for needed in ("moshi-cockpit dispatch", "agy models", "blocked", "never ends an agent"):
            self.assertIn(needed, orchestrate)


if __name__ == "__main__":
    unittest.main()
