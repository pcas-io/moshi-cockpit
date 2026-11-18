"""A headless Linux machine driven from Herdr on the Mac: install without Herdr.app, uninstall naming the token
store. Temp HOME only; the runner fails on any call, so nothing is started."""

from __future__ import annotations

import argparse
import io
import json
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path

from cockpit import cli, installer
from cockpit.agents import AgentContext
from cockpit.backup import BackupStore
from cockpit.paths import Paths
from cockpit.secrets.linux import FileStore
from tests.helpers import MOSHI


def headless_paths(test) -> Paths:
    tmp = tempfile.TemporaryDirectory()
    test.addCleanup(tmp.cleanup)
    home = Path(tmp.name)
    (home / ".config").mkdir(mode=0o700)
    return Paths(home=home, herdr_app_home=home / ".config" / "herdr-app", applications=home / "none",
                 headless=True)


def no_runner(args, **_kwargs):
    raise AssertionError(f"a headless install runs nothing: {args}")


class InstallTest(unittest.TestCase):
    def test_install_on_a_headless_machine_needs_no_herdr_app(self):
        paths = headless_paths(self)
        ctx = AgentContext(paths=paths, backup=BackupStore(paths.state), keychain=FileStore(paths.secrets_file))
        notes = installer.run(ctx, installer.InstallOptions(moshi=MOSHI), runner=no_runner,
                              say=lambda text: self.fail(f"nothing to say: {text}"),
                              sleep=lambda _s: self.fail("no waiting"))
        self.assertEqual(notes[0], installer.HEADLESS_NOTE)
        self.assertEqual(json.loads(paths.mesh_config.read_text())["servers"]["moshi"], MOSHI)
        self.assertTrue((paths.local_bin / "moshi-cockpit").is_symlink())
        self.assertFalse(paths.herdr_app_home.exists())
        self.assertFalse(any("Herdr" in note for note in notes[1:]))

    def test_a_second_headless_install_is_unchanged(self):
        paths = headless_paths(self)
        ctx = AgentContext(paths=paths, backup=BackupStore(paths.state), keychain=FileStore(paths.secrets_file))
        installer.run(ctx, installer.InstallOptions(moshi=MOSHI), runner=no_runner)
        notes = installer.run(ctx, installer.InstallOptions(moshi=MOSHI), runner=no_runner)
        self.assertEqual(notes, [installer.HEADLESS_NOTE, f"Servers: moshi {MOSHI}, unchanged",
                                 "Launchers linked: unchanged"])


class UninstallTest(unittest.TestCase):
    def test_uninstall_on_headless_names_the_store(self):
        paths = headless_paths(self)
        FileStore(paths.secrets_file).write("moshi-device", "device", "bt_device_token_1234567890")
        out = io.StringIO()
        with redirect_stdout(out):
            self.assertEqual(cli.cmd_uninstall(argparse.Namespace(), paths), 0)
        text = out.getvalue()
        self.assertIn("the tokens in file", text)
        self.assertIn("secrets.json", text)
        self.assertIn("(or the moshi-cockpit entries in your keyring)", text)
        self.assertNotIn("Keychain", text)


if __name__ == "__main__":
    unittest.main()
