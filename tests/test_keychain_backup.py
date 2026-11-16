import os
import stat
import unittest

from cockpit.backup import BackupStore
from cockpit.keychain import Keychain, KeychainError, strict_in_dump
from tests.helpers import FakeSecurity, temp_paths

TOKEN = "bt_SECRETVALUE_1234567890"


class KeychainTest(unittest.TestCase):
    def test_write_sends_secret_over_stdin_never_argv(self):
        fake = FakeSecurity()
        Keychain(fake).write("moshi-vibe", "vibe", TOKEN)
        self.assertEqual(fake.entries[("moshi-vibe", "vibe")], TOKEN)
        self.assertFalse(fake.argv_contains(TOKEN))

    def test_strict_sets_empty_trusted_app_list(self):
        fake = FakeSecurity()
        keychain = Keychain(fake)
        keychain.write("moshi-vibe", "vibe", TOKEN, strict=True)
        self.assertTrue(keychain.is_strict("moshi-vibe", "vibe"))

    def test_strict_replaces_an_existing_entry_instead_of_merging_its_acl(self):
        fake = FakeSecurity({("moshi-vibe", "vibe"): "bt_OLDVALUE_123456789"})  # trusted: /usr/bin/security
        keychain = Keychain(fake)
        self.assertFalse(keychain.is_strict("moshi-vibe", "vibe"))
        keychain.write("moshi-vibe", "vibe", TOKEN, strict=True)
        self.assertTrue(keychain.is_strict("moshi-vibe", "vibe"))
        self.assertEqual(fake.entries[("moshi-vibe", "vibe")], TOKEN)
        self.assertTrue(any(args[:2] == ["security", "delete-generic-password"] for args, _ in fake.calls))
        self.assertFalse(fake.argv_contains(TOKEN))

    def test_strict_rewrite_survives_one_failed_add(self):
        fake = FakeSecurity({("moshi-vibe", "vibe"): "bt_OLDVALUE_123456789"})
        fake.fail_adds["moshi-vibe"] = 1
        keychain = Keychain(fake)
        keychain.write("moshi-vibe", "vibe", TOKEN, strict=True)
        self.assertEqual(fake.entries[("moshi-vibe", "vibe")], TOKEN)
        self.assertTrue(keychain.is_strict("moshi-vibe", "vibe"))
        self.assertNotIn(("moshi-vibe.pending", "vibe"), fake.entries)

    def test_strict_rewrite_never_loses_the_token_and_recovers_it(self):
        fake = FakeSecurity({("moshi-vibe", "vibe"): "bt_OLDVALUE_123456789"})
        fake.fail_adds["moshi-vibe"] = 2
        keychain = Keychain(fake)
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
            Keychain(fake).write("moshi-vibe", "vibe", TOKEN, strict=True)
        self.assertEqual(fake.entries[("moshi-vibe", "vibe")], "bt_OLDVALUE_123456789")
        self.assertNotIn(("moshi-vibe.pending", "vibe"), fake.entries)  # no weak copy left behind

    def test_unverifiable_target_keeps_the_parked_copy(self):
        fake = FakeSecurity({("moshi-vibe", "vibe"): "bt_OLDVALUE_123456789"})
        fake.weak.add("moshi-vibe")
        with self.assertRaises(KeychainError) as ctx:
            Keychain(fake).write("moshi-vibe", "vibe", TOKEN, strict=True)
        self.assertIn("moshi-vibe.pending", str(ctx.exception))
        self.assertNotIn(("moshi-vibe", "vibe"), fake.entries)  # the weak entry was removed again
        self.assertEqual(fake.entries[("moshi-vibe.pending", "vibe")], TOKEN)
        self.assertTrue(fake.strict[("moshi-vibe.pending", "vibe")])

    def test_a_stale_parked_copy_next_to_the_entry_is_reported_not_guessed(self):
        fake = FakeSecurity({("moshi-vibe", "vibe"): "bt_OLDVALUE_123456789",
                             ("moshi-vibe.pending", "vibe"): "bt_PARKEDVALUE_12345678"})
        keychain = Keychain(fake)
        self.assertTrue(keychain.has_parked("moshi-vibe", "vibe"))
        with self.assertRaises(KeychainError) as ctx:
            keychain.write("moshi-vibe", "vibe", TOKEN, strict=True)
        self.assertIn("abgebrochenen Lauf", str(ctx.exception))
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
                Keychain(fake).write("moshi-vibe", "vibe", value, strict=True)
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
            Keychain(fake).write("moshi-vibe", "vibe", TOKEN, strict=True)
        self.assertEqual(fake.entries[("moshi-vibe", "vibe")], "bt_OLDVALUE_123456789")

    def test_a_failure_reported_only_on_stderr_is_an_error(self):
        import subprocess

        def runner(args, input="", **_kw):  # `security -i` exits 0 and reports on stderr only
            return subprocess.CompletedProcess(args, 0, "", "add-generic-password: returned -25299\n")
        with self.assertRaises(KeychainError):
            Keychain(runner).write("moshi-codex", "codex", TOKEN)

    def test_strict_check_fails_when_macos_kept_the_old_trusted_apps(self):
        fake = FakeSecurity()
        keychain = Keychain(fake)
        fake.dump = lambda: ""  # entry not found in the dump: not provably strict
        with self.assertRaises(KeychainError) as ctx:
            keychain.write("moshi-vibe", "vibe", TOKEN, strict=True)
        self.assertIn("nicht gehärtet", str(ctx.exception))

    def test_exists_does_not_request_the_secret(self):
        fake = FakeSecurity({("moshi-vibe", "vibe"): TOKEN})
        self.assertTrue(Keychain(fake).exists("moshi-vibe", "vibe"))
        self.assertNotIn("-w", fake.calls[-1][0])

    def test_read_missing_entry_names_entry_not_value(self):
        with self.assertRaises(KeychainError) as ctx:
            Keychain(FakeSecurity()).read("moshi-agy", "agy")
        self.assertIn("moshi-agy", str(ctx.exception))

    def test_rejects_tokens_that_could_inject_commands(self):
        for bad in ('abc" -T "x', "short", "has space inside 123", "line\nbreak12345"):
            with self.subTest(bad=bad), self.assertRaises(KeychainError) as ctx:
                Keychain(FakeSecurity()).write("moshi-vibe", "vibe", bad)
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


if __name__ == "__main__":
    unittest.main()
