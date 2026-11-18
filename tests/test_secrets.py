import os
import stat
import unittest
from pathlib import Path

from cockpit.backup import BackupStore
from unittest import mock

from cockpit.secrets import KeychainError, UnsupportedPlatform, default_store
from cockpit.secrets.macos import MacKeychain, strict_in_dump
from tests.helpers import FakeSecurity, temp_paths

TOKEN = "bt_SECRETVALUE_1234567890"


class DefaultStoreTest(unittest.TestCase):
    def test_default_store_is_the_macos_keychain_on_darwin(self):
        store = default_store(system=lambda: "Darwin")
        self.assertIsInstance(store, MacKeychain)
        self.assertEqual(store.name, "macOS Keychain")
        for method in ("exists", "read", "write", "delete"):
            self.assertTrue(callable(getattr(store, method)), method)

    def test_default_store_refuses_other_platforms_with_a_clear_sentence(self):
        with self.assertRaises(UnsupportedPlatform) as caught:
            default_store(system=lambda: "Windows")
        self.assertEqual(str(caught.exception), "Windows is not supported yet")


class KeychainTest(unittest.TestCase):
    def test_write_sends_secret_over_stdin_never_argv(self):
        fake = FakeSecurity()
        MacKeychain(fake).write("moshi-vibe", "vibe", TOKEN)
        self.assertEqual(fake.entries[("moshi-vibe", "vibe")], TOKEN)
        self.assertFalse(fake.argv_contains(TOKEN))

    def test_strict_sets_empty_trusted_app_list(self):
        fake = FakeSecurity()
        keychain = MacKeychain(fake)
        keychain.write("moshi-vibe", "vibe", TOKEN, strict=True)
        self.assertTrue(keychain.is_strict("moshi-vibe", "vibe"))

    def test_strict_replaces_an_existing_entry_instead_of_merging_its_acl(self):
        fake = FakeSecurity({("moshi-vibe", "vibe"): "bt_OLDVALUE_123456789"})  # trusted: /usr/bin/security
        keychain = MacKeychain(fake)
        self.assertFalse(keychain.is_strict("moshi-vibe", "vibe"))
        keychain.write("moshi-vibe", "vibe", TOKEN, strict=True)
        self.assertTrue(keychain.is_strict("moshi-vibe", "vibe"))
        self.assertEqual(fake.entries[("moshi-vibe", "vibe")], TOKEN)
        self.assertTrue(any(args[:2] == ["security", "delete-generic-password"] for args, _ in fake.calls))
        self.assertFalse(fake.argv_contains(TOKEN))

    def test_strict_rewrite_survives_one_failed_add(self):
        fake = FakeSecurity({("moshi-vibe", "vibe"): "bt_OLDVALUE_123456789"})
        fake.fail_adds["moshi-vibe"] = 1
        keychain = MacKeychain(fake)
        keychain.write("moshi-vibe", "vibe", TOKEN, strict=True)
        self.assertEqual(fake.entries[("moshi-vibe", "vibe")], TOKEN)
        self.assertTrue(keychain.is_strict("moshi-vibe", "vibe"))
        self.assertNotIn(("moshi-vibe.pending", "vibe"), fake.entries)

    def test_strict_rewrite_never_loses_the_token_and_recovers_it(self):
        fake = FakeSecurity({("moshi-vibe", "vibe"): "bt_OLDVALUE_123456789"})
        fake.fail_adds["moshi-vibe"] = 2
        keychain = MacKeychain(fake)
        with self.assertRaises(KeychainError) as ctx:
            keychain.write("moshi-vibe", "vibe", TOKEN, strict=True)
        self.assertIn("moshi-vibe.pending", str(ctx.exception))
        self.assertNotIn(TOKEN, str(ctx.exception))
        self.assertEqual(fake.entries[("moshi-vibe.pending", "vibe")], TOKEN)  # parked, strict
        self.assertTrue(fake.strict[("moshi-vibe.pending", "vibe")])
        self.assertTrue(keychain.restore_parked("moshi-vibe", "vibe"))
        self.assertEqual(fake.entries[("moshi-vibe", "vibe")], TOKEN)
        self.assertTrue(keychain.is_strict("moshi-vibe", "vibe"))
        self.assertNotIn(("moshi-vibe.pending", "vibe"), fake.entries)
        self.assertFalse(fake.argv_contains(TOKEN))

    def test_unverifiable_parking_leaves_the_entry_untouched(self):
        fake = FakeSecurity({("moshi-vibe", "vibe"): "bt_OLDVALUE_123456789"})
        fake.weak.add("moshi-vibe.pending")
        with self.assertRaises(KeychainError):
            MacKeychain(fake).write("moshi-vibe", "vibe", TOKEN, strict=True)
        self.assertEqual(fake.entries[("moshi-vibe", "vibe")], "bt_OLDVALUE_123456789")
        self.assertNotIn(("moshi-vibe.pending", "vibe"), fake.entries)  # no weak copy left behind

    def test_unverifiable_target_keeps_the_parked_copy(self):
        fake = FakeSecurity({("moshi-vibe", "vibe"): "bt_OLDVALUE_123456789"})
        fake.weak.add("moshi-vibe")
        with self.assertRaises(KeychainError) as ctx:
            MacKeychain(fake).write("moshi-vibe", "vibe", TOKEN, strict=True)
        self.assertIn("moshi-vibe.pending", str(ctx.exception))
        self.assertNotIn(("moshi-vibe", "vibe"), fake.entries)  # the weak entry was removed again
        self.assertEqual(fake.entries[("moshi-vibe.pending", "vibe")], TOKEN)
        self.assertTrue(fake.strict[("moshi-vibe.pending", "vibe")])

    def test_a_stale_parked_copy_next_to_the_entry_is_reported_not_guessed(self):
        fake = FakeSecurity({("moshi-vibe", "vibe"): "bt_OLDVALUE_123456789",
                             ("moshi-vibe.pending", "vibe"): "bt_PARKEDVALUE_12345678"})
        keychain = MacKeychain(fake)
        self.assertTrue(keychain.has_parked("moshi-vibe", "vibe"))
        with self.assertRaises(KeychainError) as ctx:
            keychain.write("moshi-vibe", "vibe", TOKEN, strict=True)
        self.assertIn("interrupted run", str(ctx.exception))
        self.assertEqual(fake.entries[("moshi-vibe", "vibe")], "bt_OLDVALUE_123456789")
        self.assertEqual(fake.entries[("moshi-vibe.pending", "vibe")], "bt_PARKEDVALUE_12345678")
        self.assertFalse(keychain.restore_parked("moshi-vibe", "vibe"))

    def test_parallel_strict_writes_are_serialized(self):
        import threading
        fake = FakeSecurity({("moshi-vibe", "vibe"): "bt_OLDVALUE_123456789"})
        fake.delay = 0.002
        errors = []

        def write(value):
            try:
                MacKeychain(fake).write("moshi-vibe", "vibe", value, strict=True)
            except KeychainError as exc:  # pragma: no cover - the assertion below shows it
                errors.append(str(exc))
        values = ["bt_FIRSTVALUE_1234567890", "bt_SECONDVALUE_123456789"]
        threads = [threading.Thread(target=write, args=(v,)) for v in values]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual(errors, [])
        self.assertIn(fake.entries[("moshi-vibe", "vibe")], values)
        self.assertTrue(fake.strict[("moshi-vibe", "vibe")])
        self.assertNotIn(("moshi-vibe.pending", "vibe"), fake.entries)

    def test_nothing_is_deleted_when_parking_fails(self):
        fake = FakeSecurity({("moshi-vibe", "vibe"): "bt_OLDVALUE_123456789"})
        fake.fail_adds["moshi-vibe.pending"] = 1
        with self.assertRaises(KeychainError):
            MacKeychain(fake).write("moshi-vibe", "vibe", TOKEN, strict=True)
        self.assertEqual(fake.entries[("moshi-vibe", "vibe")], "bt_OLDVALUE_123456789")

    def test_a_failure_reported_only_on_stderr_is_an_error(self):
        import subprocess

        def runner(args, input="", **_kw):  # `security -i` exits 0 and reports on stderr only
            return subprocess.CompletedProcess(args, 0, "", "add-generic-password: returned -25299\n")
        with self.assertRaises(KeychainError):
            MacKeychain(runner).write("moshi-codex", "codex", TOKEN)

    def test_strict_check_fails_when_macos_kept_the_old_trusted_apps(self):
        fake = FakeSecurity()
        keychain = MacKeychain(fake)
        fake.dump = lambda: ""  # entry not found in the dump: not provably strict
        with self.assertRaises(KeychainError) as ctx:
            keychain.write("moshi-vibe", "vibe", TOKEN, strict=True)
        self.assertIn("not hardened", str(ctx.exception))

    def test_exists_does_not_request_the_secret(self):
        fake = FakeSecurity({("moshi-vibe", "vibe"): TOKEN})
        self.assertTrue(MacKeychain(fake).exists("moshi-vibe", "vibe"))
        self.assertNotIn("-w", fake.calls[-1][0])

    def test_read_missing_entry_names_entry_not_value(self):
        with self.assertRaises(KeychainError) as ctx:
            MacKeychain(FakeSecurity()).read("moshi-agy", "agy")
        self.assertIn("moshi-agy", str(ctx.exception))

    def test_rejects_tokens_that_could_inject_commands(self):
        for bad in ('abc" -T "x', "short", "has space inside 123", "line\nbreak12345"):
            with self.subTest(bad=bad), self.assertRaises(KeychainError) as ctx:
                MacKeychain(FakeSecurity()).write("moshi-vibe", "vibe", bad)
            self.assertNotIn(bad, str(ctx.exception))


def _item(service, account, *entries):
    acl = "".join(f"    entry {i}:\n        authorizations ({len(a.split())}): {a}\n"
                  f"        don't-require-password\n        description: {service}\n        {apps}\n"
                  for i, (a, apps) in enumerate(entries))
    return (f'keychain: "login.keychain-db"\nattributes:\n    "acct"<blob>="{account}"\n'
            f'    "svce"<blob>="{service}"\naccess: {len(entries)} entries\n{acl}')


DECRYPT = "decrypt derive export_clear export_wrapped mac sign"


class StrictDumpTest(unittest.TestCase):
    def test_all_decrypting_entries_must_have_no_application(self):
        strict = (DECRYPT, "applications (0):")
        self.assertTrue(strict_in_dump(_item("s", "a", strict, ("encrypt", "applications: <null>")), "s", "a"))
        cases = {
            "second decrypt ACL trusts an app": (strict, (DECRYPT, "applications (1):\n            0: /usr/bin/security")),
            "any with null apps": (strict, ("any", "applications: <null>")),
            "decrypt with null apps": ((DECRYPT, "applications: <null>"),),
            "no decrypting entry": (("encrypt", "applications: <null>"),),
            "decrypt without applications line": ((DECRYPT, ""),),
        }
        for name, entries in cases.items():
            with self.subTest(name):
                self.assertFalse(strict_in_dump(_item("s", "a", *entries), "s", "a"))

    def test_every_matching_item_counts_and_others_do_not(self):
        strict = (DECRYPT, "applications (0):")
        weak = (DECRYPT, "applications: <null>")
        dump = _item("s", "a", strict) + _item("s", "a", weak)
        self.assertFalse(strict_in_dump(dump, "s", "a"))
        self.assertTrue(strict_in_dump(_item("s", "a", strict) + _item("s2", "a", weak), "s", "a"))
        self.assertFalse(strict_in_dump(_item("other", "a", strict), "s", "a"))


class BackupStoreTest(unittest.TestCase):
    def setUp(self):
        self.paths = temp_paths(self)
        self.store = BackupStore(self.paths.state, clock=lambda: 1_700_000_000)

    def test_write_is_idempotent(self):
        target = self.paths.home / "a.txt"
        self.assertTrue(self.store.write_text(target, "one"))
        self.assertFalse(self.store.write_text(target, "one"))
        self.assertEqual(len(self.store.entries()), 1)

    def test_restore_brings_back_original_and_removes_created(self):
        existing = self.paths.home / "existing.txt"
        existing.write_text("original")
        created = self.paths.home / "new.txt"
        self.store.write_text(existing, "changed")
        self.store.write_text(existing, "changed again")
        self.store.write_text(created, "fresh")
        self.store.restore_all()
        self.assertEqual(existing.read_text(), "original")
        self.assertFalse(created.exists())
        self.assertEqual(self.store.entries(), [])

    def test_secret_file_mode_survives_backup(self):
        secret = self.paths.home / "mcp_config.json"
        self.store.write_text(secret, "token-a", mode=0o600)
        self.store.write_text(secret, "token-b", mode=0o600)
        self.assertEqual(stat.S_IMODE(secret.stat().st_mode), 0o600)

    def test_backup_copy_of_secret_file_stays_private(self):
        secret = self.paths.home / "mcp_config.json"
        secret.write_text("token-a")
        os.chmod(secret, 0o600)
        self.store.write_text(secret, "token-b", mode=0o600)
        backups = [p for p in self.paths.home.iterdir() if ".bak-" in p.name]
        self.assertEqual(len(backups), 1)
        self.assertEqual(stat.S_IMODE(backups[0].stat().st_mode), 0o600)

    def test_symlink_restore(self):
        link = self.paths.local_bin / "vibe-mesh"
        link.parent.mkdir(parents=True)
        link.write_text("old launcher")
        self.store.symlink(self.paths.home / "repo-launcher", link)
        self.assertTrue(link.is_symlink())
        self.store.restore_all()
        self.assertFalse(link.is_symlink())
        self.assertEqual(link.read_text(), "old launcher")

    def test_a_missing_backup_keeps_the_current_file_and_the_rest_is_restored(self):
        first, second = self.paths.home / "first.txt", self.paths.home / "second.txt"
        first.write_text("original 1")
        second.write_text("original 2")
        self.store.write_text(first, "changed 1")
        self.store.write_text(second, "changed 2")
        Path(self.store.entries()[1]["backup"]).unlink()  # second's backup is gone
        notes = self.store.restore_all()
        self.assertEqual(second.read_text(), "changed 2")  # not deleted
        self.assertEqual(first.read_text(), "original 1")
        self.assertIn(f"skipped, its backup is missing: {second}", notes)
        self.assertEqual(self.store.entries(), [])

    def test_restore_keeps_everything_inside_a_kept_folder(self):
        kept_dir = self.paths.config / "moshi-cockpit"
        mine, other = kept_dir / "config.json", self.paths.home / "settings.json"
        self.store.write_text(mine, "{}", mode=0o600)
        self.store.write_text(other, "x")
        notes = self.store.restore_all(keep=kept_dir)
        self.assertTrue(mine.is_file())
        self.assertFalse(other.exists())
        self.assertEqual(notes, [f"removed: {other}"])
        self.assertEqual(self.store.entries(), [])


class UninstallTest(unittest.TestCase):
    """`uninstall` undoes the changes to Herdr and keeps the person's own settings and the Keychain."""

    def test_uninstall_keeps_the_config_folder_and_says_what_stays(self):
        import io
        from contextlib import redirect_stdout
        from cockpit import cli, config
        paths = temp_paths(self)
        store = BackupStore(paths.state)
        config.save_servers(paths, store, {"moshi": "https://moshi.example.test/mcp"})
        config.save_rules(paths, store, "No customer data.")
        store.write_text(paths.agents_file, '{"agents": []}\n', mode=0o600)
        store.write_text(paths.herdr_settings, '{"telemetry": false}\n')
        out = io.StringIO()
        with mock.patch("cockpit.cli.Paths.from_env", return_value=paths), redirect_stdout(out):
            self.assertEqual(cli.main(["uninstall"]), 0)
        for kept in (paths.mesh_config, paths.rules_file, paths.agents_file):
            self.assertTrue(kept.is_file(), kept)
        self.assertFalse(paths.herdr_settings.exists())
        text = out.getvalue()
        self.assertIn(f"removed: {paths.herdr_settings}", text)
        self.assertNotIn("config.json:", text.replace("config.json, ", ""))  # no line restores it
        self.assertIn("Kept: your settings in ~/.config/moshi-cockpit/ (config.json, agents.json, rules.md)", text)
        self.assertIn("Keychain", text)
        self.assertIn("rm -rf ~/.config/moshi-cockpit", text)
        self.assertIn("Earlier copies of your settings (*.bak-…) stay in ~/.config/moshi-cockpit/", text)


if __name__ == "__main__":
    unittest.main()
