"""`harness install`: the vendor's own installer, only after a yes, with a fake runner (nothing real runs)."""

import unittest
from unittest import mock

from cockpit import cli, harness_install
from cockpit.harness_install import INSTALL, InstallIO, InstallSpec
from cockpit.harnesses import HARNESSES
from tests.helpers import temp_paths


class Session:
    """Scripted terminal: answers, collected output, and a runner that can drop the binary in place."""

    def __init__(self, answers=(), interactive=True):
        self.answers, self.said, self.asked, self.ran = list(answers), [], [], []
        self.io = InstallIO(ask=self.ask, say=self.said.append, interactive=interactive)

    def ask(self, prompt):
        self.asked.append(prompt)
        return self.answers.pop(0)

    def runner(self, then=lambda: None, code=0):
        def run(argv):
            self.ran.append(tuple(argv))
            then()
            return code
        return run

    @property
    def text(self):
        return "\n".join(self.said)


class HarnessInstallTest(unittest.TestCase):
    def setUp(self):
        self.paths = temp_paths(self)
        # installed() looks at PATH too: this machine's own binaries must not count
        patch = mock.patch("cockpit.harnesses.base.shutil.which", return_value=None)
        patch.start()
        self.addCleanup(patch.stop)
        self.harness = HARNESSES["codex"]
        self.spec = INSTALL["codex"]
        self.binary = self.paths.local_bin / self.harness.binary

    def appear(self):
        self.binary.parent.mkdir(parents=True, exist_ok=True)
        self.binary.write_text("#!/bin/sh\n")

    def go(self, session, runner, which=lambda tool: "/usr/bin/" + tool):
        return harness_install.install(self.harness, session.io, runner, self.paths, which=which)

    def test_commands_and_sources_are_the_pinned_vendor_ones(self):
        self.assertEqual(set(INSTALL), set(HARNESSES))
        self.assertEqual({n: sp.argv[2] for n, sp in INSTALL.items()}, {
            "claude": "curl -fsSL https://claude.ai/install.sh | bash",
            "codex": "curl -fsSL https://chatgpt.com/codex/install.sh | sh",
            "vibe": "curl -LsSf https://mistral.ai/vibe/install.sh | bash",
            "agy": "curl -fsSL https://antigravity.google/cli/install.sh | bash",
            "grok": "curl -fsSL https://x.ai/cli/install.sh | bash"})
        prefixes = ("https://code.claude.com/", "https://github.com/openai/", "https://github.com/mistralai/",
                    "https://antigravity.google/", "https://docs.x.ai/")
        for name, spec in INSTALL.items():
            self.assertTrue(spec.source_url.startswith(prefixes), name)

    def test_end_of_input_at_the_prompt_is_a_no(self):
        s = Session()

        def eof(prompt):
            raise EOFError

        s.io = InstallIO(ask=eof, say=s.said.append, interactive=True)
        self.assertEqual(self.go(s, s.runner()), 1)
        self.assertEqual(s.ran, [])
        self.assertIn("Cancelled, nothing was installed.", s.text)

    def test_install_asks_before_running(self):
        s = Session(["n"])
        self.assertEqual(self.go(s, s.runner()), 1)
        self.assertEqual(s.ran, [])
        self.assertEqual(s.said[0], f"This runs the official installer of codex: {harness_install.shown_command(self.spec)}")
        self.assertEqual(s.asked, ["Continue? [y/N] "])

    def test_empty_answer_means_no(self):
        s = Session([""])
        self.assertEqual(self.go(s, s.runner()), 1)
        self.assertEqual(s.ran, [])

    def test_without_a_terminal_it_refuses_and_runs_nothing(self):
        s = Session(interactive=False)
        self.assertEqual(self.go(s, s.runner()), 1)
        self.assertEqual((s.ran, s.asked), ([], []))
        self.assertIn("needs a terminal", s.text)

    def test_install_reports_success_when_the_binary_appears(self):
        s = Session(["y"])
        self.assertEqual(self.go(s, s.runner(self.appear)), 0)
        self.assertEqual(s.ran, [self.spec.argv])
        self.assertIn("codex is installed.", s.text)

    def test_exit_zero_without_a_binary_is_a_failure(self):
        s = Session(["y"])
        self.assertEqual(self.go(s, s.runner()), 1)
        self.assertIn("not on your PATH", s.text)

    def test_installer_failure_is_reported_with_its_code(self):
        s = Session(["yes"])
        self.assertEqual(self.go(s, s.runner(code=7)), 1)
        self.assertIn("exited with code 7", s.text)

    def test_vendor_without_a_script_gets_a_link(self):
        self.harness = HARNESSES["codex"]
        original = INSTALL["codex"]
        INSTALL["codex"] = InstallSpec((), "https://vendor.example.test/get", ())
        self.addCleanup(INSTALL.__setitem__, "codex", original)
        s = Session()
        self.assertEqual(self.go(s, s.runner()), 1)
        self.assertEqual(s.said, ["Install codex from https://vendor.example.test/get, then reopen this dialog."])
        self.assertEqual(s.ran, [])

    def test_missing_prerequisite_is_named_before_asking(self):
        s = Session()
        self.assertEqual(self.go(s, s.runner(), which=lambda tool: None), 1)
        self.assertIn("needs curl", s.text)
        self.assertEqual((s.ran, s.asked), ([], []))

    def test_already_installed_runs_nothing(self):
        self.appear()
        s = Session()
        self.assertEqual(self.go(s, s.runner()), 0)
        self.assertEqual((s.ran, s.asked), ([], []))

    def test_scripts_are_the_published_one_liners_in_sh_c(self):
        for name, spec in INSTALL.items():
            self.assertEqual(spec.argv[:2], ("/bin/sh", "-c"), name)
            self.assertTrue(spec.argv[2].startswith("curl "), name)
            self.assertEqual(harness_install.shown_command(spec), spec.argv[2])

    def test_the_offer_tells_the_panel_which_harnesses_are_installed(self):
        from cockpit import wizard
        self.appear()  # codex in ~/.local/bin, nothing else
        offer = {h["name"]: h for h in wizard.harness_offer(self.paths)}
        self.assertEqual({name: h["installed"] for name, h in offer.items()},
                         {"claude": False, "codex": True, "vibe": False, "agy": False, "grok": False})
        self.assertTrue(offer["codex"]["available"])

    def test_cli_rejects_an_unknown_harness(self):
        with self.assertRaises(SystemExit):
            cli.build_parser().parse_args(["harness", "install", "nope"])
        args = cli.build_parser().parse_args(["harness", "install", "grok"])
        self.assertEqual((args.command, args.name), ("harness", "grok"))


class HarnessTabTest(unittest.TestCase):
    """`harness install` runs in a Herdr tab, which closes with the process: on a terminal it waits for Enter."""

    def setUp(self):
        self.paths = temp_paths(self)

    def run_cli(self, code, terminal):
        prompts = []
        with mock.patch("cockpit.cli.Paths.from_env", return_value=self.paths), \
                mock.patch("cockpit.cli._on_terminal", return_value=terminal), \
                mock.patch.object(harness_install, "run_cli", return_value=code) as ran, \
                mock.patch("builtins.input", lambda prompt="": prompts.append(prompt) or ""):
            result = cli.main(["harness", "install", "codex"])
        self.assertEqual(ran.call_args.args[2].interactive, terminal)
        return result, prompts

    def test_on_a_terminal_success_and_failure_both_wait_for_enter(self):
        for code in (0, 1):
            with self.subTest(code=code):
                self.assertEqual(self.run_cli(code, True), (code, ["\nPress Enter to close this tab. "]))

    def test_without_a_terminal_nothing_waits(self):
        self.assertEqual(self.run_cli(1, False), (1, []))


if __name__ == "__main__":
    unittest.main()
