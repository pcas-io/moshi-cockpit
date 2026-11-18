"""A headless Linux machine driven from Herdr on the Mac: install without Herdr.app, uninstall naming the token
store, doctor without the Herdr.app checks, the state fields of `agent list --json`. Temp HOME only; the runner
fails on any call, so nothing is started."""

from __future__ import annotations

import argparse
import io
import json
import os
import subprocess
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path

import cockpit
from cockpit import cli, compat, connect, doctor, installer
from cockpit.agents import AgentContext
from cockpit.backup import BackupStore
from cockpit.paths import Paths
from cockpit.secrets.linux import FileStore
from cockpit.secrets.macos import MacKeychain
from tests.helpers import MOSHI, FakeSecurity, bind_non_agent, configure, temp_paths


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


DEVICE_KEY = "dk_device_key_1234567890"
SKIPPED = ("herdr", "telemetry", "permissions", "mods", "cockpit", "menu", "keychain-parked")


class DoctorTest(unittest.TestCase):
    def setUp(self):
        self.paths = headless_paths(self)
        self.store = FileStore(self.paths.secrets_file)
        configure(self.paths, servers={})

    def doctor(self, core="0.1.21"):
        return doctor.DoctorContext(paths=self.paths, keychain=self.store, probe=lambda _url: (401, "Bearer"),
                                    herdr_versions=lambda _p: self.fail("no Herdr.app on a headless machine"),
                                    core_version=lambda _p: core)

    def by_name(self, **options):
        return {r.name: r for r in doctor.run(self.doctor(**options))}

    def test_headless_doctor_skips_herdr_app_checks(self):
        names = list(self.by_name())
        for gone in SKIPPED:
            self.assertNotIn(gone, names)
        self.assertIn("store", names)
        self.assertIn("herdr-core", names)
        self.assertEqual(self.by_name()["herdr-core"].status, doctor.OK)

    def test_headless_doctor_reports_the_store(self):
        store = self.by_name()["store"]
        self.assertEqual(store.status, doctor.OK)
        self.assertIn("secrets.json", store.detail)
        self.store.write(*connect.DEVICE_ENTRY, DEVICE_KEY)
        os.chmod(self.paths.secrets_file, 0o644)
        results = doctor.run(self.doctor())
        store = {r.name: r for r in results}["store"]
        self.assertEqual(store.status, doctor.FAIL)
        self.assertEqual(doctor.quiet(results), f"{self.paths.secrets_file} is accessible by other users; fix it with: "
                                                f"chmod 600 {self.paths.secrets_file}")
        self.assertNotIn(DEVICE_KEY, store.detail)

    def test_a_broken_store_never_ends_the_doctor_run(self):
        configure(self.paths, servers={"moshi": MOSHI}, agents=[{"name": "vibe", "harness": "vibe"}])
        bind_non_agent(self.paths)
        elsewhere = self.paths.home / "elsewhere.json"
        elsewhere.write_text("{}")
        self.paths.secrets_file.parent.mkdir(parents=True, exist_ok=True)
        os.symlink(elsewhere, self.paths.secrets_file)
        results = doctor.run(self.doctor())
        by_name = {r.name: r for r in results}
        self.assertEqual(by_name["store"].status, doctor.FAIL)
        self.assertIn("is a symlink", by_name["store"].detail)
        self.assertIn("endpoint-moshi", by_name)  # the checks after the store still ran
        self.assertEqual(doctor.exit_code(results), 1)
        self.assertEqual(sum("is a symlink" in r.detail for r in results), 1)

    def test_missing_herdr_core_only_warns(self):
        results = doctor.run(self.doctor(core=None))
        core = {r.name: r for r in results}["herdr-core"]
        self.assertEqual(core.status, doctor.WARN)
        self.assertIn("Machines", core.detail)
        self.assertEqual(doctor.exit_code(results), 0)

    def test_core_version_is_read_from_the_binary(self):
        calls = []
        answer = ["herdr-core 0.1.21\n"]

        def runner(args, **kwargs):
            calls.append((args, kwargs))
            return subprocess.CompletedProcess(args, 0, answer[0], "")
        no_path = lambda _name: None  # noqa: E731
        self.assertIsNone(compat.headless_core_version(self.paths, runner=runner, which=no_path))
        self.assertEqual(calls, [])
        binary = self.paths.local_bin / "herdr-core"
        binary.parent.mkdir(parents=True)
        binary.write_text("")
        self.assertEqual(compat.headless_core_version(self.paths, runner=runner, which=no_path), "0.1.21")
        self.assertEqual(calls[0][0], [str(binary), "--version"])
        self.assertEqual(calls[0][1]["timeout"], 5)
        self.assertEqual(calls[0][1]["stdin"], subprocess.DEVNULL)
        answer[0] = "garbage\n"
        self.assertEqual(compat.headless_core_version(self.paths, runner=runner, which=no_path), compat.UNKNOWN)

    def test_mac_doctor_gains_only_the_store_line(self):
        paths = temp_paths(self)
        ctx = doctor.DoctorContext(paths=paths, keychain=MacKeychain(FakeSecurity()))
        self.assertEqual(doctor.check_store(ctx), [doctor.Check("store", doctor.OK, "tokens in macOS Keychain")])
        self.assertEqual(doctor.CHECKS[doctor.CHECKS.index(doctor.check_config) + 1], doctor.check_store)
        self.assertNotIn(doctor.check_herdr_core, doctor.CHECKS)


class AgentListTest(unittest.TestCase):
    def agent_list(self, ctx):
        out = io.StringIO()
        with redirect_stdout(out):
            self.assertEqual(cli._agent_list(ctx, argparse.Namespace(json=True)), 0)
        return json.loads(out.getvalue())

    def test_agent_list_json_has_version_and_connected(self):
        paths = headless_paths(self)
        configure(paths, servers={"moshi": MOSHI})
        ctx = AgentContext(paths=paths, backup=BackupStore(paths.state), keychain=FileStore(paths.secrets_file))
        data = self.agent_list(ctx)
        self.assertIs(data["connected"], False)
        self.assertEqual(data["version"], cockpit.__version__)
        self.assertEqual(set(data), {"agents", "harnesses", "servers", "version", "connected"})
        ctx.keychain.write(*connect.DEVICE_ENTRY, DEVICE_KEY)
        self.assertIs(self.agent_list(ctx)["connected"], False)  # stored, but bound to no server
        bind_non_agent(paths)
        self.assertIs(self.agent_list(ctx)["connected"], True)


if __name__ == "__main__":
    unittest.main()
