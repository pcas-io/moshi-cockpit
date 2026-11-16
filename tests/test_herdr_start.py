"""The installer uses the Herdr the person installed from herdr.dev and starts it itself: one run, no second
install. No real app is ever opened."""

import io
import json
import subprocess
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest import mock

from cockpit import cli, defaults, installer
from tests.helpers import MAIN_LAYOUT as MAIN, herdr_app
from tests.test_agents_doctor import context

LAYOUT_SKIPPED = "Layout cockpit: übersprungen"  # defaults.py's own note (translated in a later change)


class Clock:
    def __init__(self):
        self.time = 0.0

    def now(self):
        return self.time

    def sleep(self, seconds):
        self.time += seconds


def result(code=0, out=""):
    return subprocess.CompletedProcess([], code, out, "")


class FakeHerdr:
    """`open` writes herdr's layout, as a first start does; `status` answers from a script, then stays ready."""

    def __init__(self, paths, statuses=(), opens=True, writes_layout=True, switch=0):
        self.paths, self.statuses, self.opens, self.writes_layout, self.switch = paths, list(statuses), opens, writes_layout, switch
        self.calls = []
        self.settings_at_open = None

    def __call__(self, args, **_):
        self.calls.append(list(args))
        if args[0] == "open":
            self.settings_at_open = json.loads(self.paths.herdr_settings.read_text())
            if self.writes_layout:
                layouts = self.paths.herdr_app_home / "layouts"
                layouts.mkdir(parents=True, exist_ok=True)
                (layouts / "main.json").write_text(json.dumps(MAIN))
            return result(0 if self.opens else 1)
        if args[1] == "do":
            return result(self.switch)
        answer = self.statuses.pop(0) if self.statuses else result(0, json.dumps({"layout": "main"}))
        if isinstance(answer, Exception):
            raise answer
        return answer

    def called(self, word):
        return [c for c in self.calls if word in c]


class StartTest(unittest.TestCase):
    def setUp(self):
        self.ctx, _ = context(self)
        self.clock = Clock()
        self.said = []
        herdr_app(self.ctx.paths, ran_before=False)

    def install(self, fake, **options):
        return installer.run(self.ctx, installer.InstallOptions(**options), runner=fake,
                             say=self.said.append, sleep=self.clock.sleep, now=self.clock.now)

    def layout(self):
        return self.ctx.paths.herdr_app_home / "layouts" / "cockpit.json"

    def test_a_first_start_finishes_in_one_run(self):
        fake = FakeHerdr(self.ctx.paths, statuses=[result(1), result(1)])
        notes = self.install(fake)
        self.assertEqual(fake.calls[0], ["open", str(self.ctx.paths.herdr_app_bundle)])
        self.assertEqual(len(fake.called("open")), 1)
        self.assertEqual(len(fake.called("status")), 3)
        self.assertTrue(self.layout().is_file())
        self.assertEqual(fake.calls[-1][1:], ["do", "layout.switch", '{"name": "cockpit"}'])
        self.assertEqual(notes[-1], installer.LAYOUT_SHOWN)
        self.assertFalse(any("again" in n or n.startswith(LAYOUT_SKIPPED) for n in notes))
        self.assertEqual(len(self.said), 1)  # the wait is announced, it never looks like a hang

    def test_telemetry_is_off_before_herdr_runs_for_the_first_time(self):
        fake = FakeHerdr(self.ctx.paths)
        self.install(fake)
        self.assertIs(fake.settings_at_open["telemetry"], False)

    def test_a_herdr_that_ran_before_is_left_alone(self):
        layouts = self.ctx.paths.herdr_app_home / "layouts"
        layouts.mkdir(parents=True)
        (layouts / "main.json").write_text(json.dumps(MAIN))
        fake = FakeHerdr(self.ctx.paths)
        self.install(fake)
        self.assertEqual(fake.calls, [])  # not opened, not asked, the layout on screen stays
        self.assertTrue(self.layout().is_file())
        self.assertEqual(self.said, [])

    def test_install_refuses_without_herdr_app_and_names_herdr_dev(self):
        ctx, _ = context(self)  # nothing in Applications
        fake = FakeHerdr(ctx.paths)
        with self.assertRaises(ValueError) as caught:
            installer.run(ctx, installer.InstallOptions(), runner=fake, say=self.said.append,
                          sleep=self.clock.sleep, now=self.clock.now)
        self.assertEqual(str(caught.exception), "Install Herdr from https://herdr.dev first, then run this again.")
        self.assertEqual(fake.calls, [])  # nothing downloaded, opened or asked
        self.assertFalse(ctx.paths.herdr_settings.exists())
        self.assertFalse((ctx.paths.local_bin / "moshi-cockpit").exists())
        with mock.patch("cockpit.cli.Paths.from_env", return_value=ctx.paths), \
                redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()) as err:
            self.assertEqual(cli.main(["install"]), 1)
        self.assertIn("Install Herdr from https://herdr.dev first", err.getvalue())

    def test_a_timeout_ends_with_one_sentence_that_says_what_to_do(self):
        fake = FakeHerdr(self.ctx.paths, statuses=[result(1)] * 500)
        notes = self.install(fake)
        self.assertEqual(notes[-1], installer.HERDR_NOT_READY)
        self.assertEqual(sum("Herdr" in n and "again" in n for n in notes), 1)
        self.assertFalse(any(n.startswith(LAYOUT_SKIPPED) for n in notes))
        self.assertGreaterEqual(self.clock.time, installer.START_TIMEOUT)
        self.assertLessEqual(self.clock.time, installer.START_TIMEOUT + installer.POLL_SECONDS)
        self.assertFalse(self.layout().exists())
        self.assertEqual(fake.called("do"), [])
        self.assertTrue(any("Launchers" in n for n in notes))  # the rest of the setup is done

    def test_it_waits_for_the_layout_before_asking_the_window(self):
        fake = FakeHerdr(self.ctx.paths, writes_layout=False)
        notes = self.install(fake)
        self.assertEqual(notes[-1], installer.HERDR_NOT_READY)
        self.assertEqual(fake.called("status"), [])

    def test_a_failed_open_does_not_wait(self):
        fake = FakeHerdr(self.ctx.paths, opens=False, writes_layout=False)
        notes = self.install(fake)
        self.assertEqual(notes[-1], installer.HERDR_NOT_READY)
        self.assertEqual(self.clock.time, 0)

    def test_only_a_named_layout_counts_as_ready(self):
        waiting = [result(1), result(0, "kein json"), result(0, "[]"), result(0, '{"layout": ""}'), result(0, '{"layout": null}'),
                   subprocess.TimeoutExpired("x", 5), OSError("weg")]
        fake = FakeHerdr(self.ctx.paths, statuses=waiting)
        notes = self.install(fake)
        self.assertEqual(len(fake.called("status")), len(waiting) + 1)
        self.assertEqual(notes[-1], installer.LAYOUT_SHOWN)

    def test_a_failed_switch_names_the_key(self):
        fake = FakeHerdr(self.ctx.paths, switch=1)
        notes = self.install(fake)
        self.assertIn("⌘⇧L", notes[-1])
        self.assertTrue(self.layout().is_file())

    def test_the_status_call_uses_the_checked_bundle_not_the_path(self):
        fake = FakeHerdr(self.ctx.paths)
        self.install(fake)
        binary = Path(fake.called("status")[0][0])
        self.assertEqual(binary, self.ctx.paths.herdr_app_bundle / "Contents" / "MacOS" / "Herdr")

    def test_the_main_layout_check_rejects_a_broken_file(self):
        layouts = self.ctx.paths.herdr_app_home / "layouts"
        layouts.mkdir(parents=True)
        for text in ("kaputt", "{}", '{"type": "row", "children": []}', '{"type": "column", "children": [1]}'):
            with self.subTest(text=text):
                (layouts / "main.json").write_text(text)
                self.assertFalse(defaults.main_layout_ready(self.ctx.paths))
        (layouts / "main.json").write_text(json.dumps(MAIN))
        self.assertTrue(defaults.main_layout_ready(self.ctx.paths))


class CommandLineTest(unittest.TestCase):
    def run_cli(self, notes):
        from cockpit import cli
        args = mock.Mock(moshi=None, ocellio=None)
        ctx, _ = context(self)
        with mock.patch.object(cli, "_context", return_value=ctx), mock.patch.object(cli.installer, "run", return_value=notes), \
                mock.patch.object(cli.doctor, "run", return_value=[]), mock.patch("builtins.print") as printed:
            code = cli.cmd_install(args, ctx.paths)
        return code, " ".join(str(c.args[0]) for c in printed.call_args_list)

    def test_a_timeout_replaces_the_next_step(self):
        code, text = self.run_cli(["x", installer.HERDR_NOT_READY])
        self.assertEqual(code, 0)
        self.assertIn(installer.HERDR_NOT_READY, text)
        self.assertNotIn("Done", text)

    def test_a_normal_run_ends_quietly(self):
        self.assertIn("Done", self.run_cli(["x"])[1])


if __name__ == "__main__":
    unittest.main()
