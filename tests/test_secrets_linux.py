import json
import os
import stat
import subprocess
import tempfile
import threading
import unittest
from pathlib import Path
from typing import Dict, List, Optional, Tuple
from unittest import mock

from cockpit.paths import Paths
from cockpit.secrets import KeychainError, UnsupportedPlatform, default_store
from cockpit.secrets.linux import (ATTR_APP, MARKER_NAME, NO_ANSWER, PROBE_ENTRY, PROBE_TIMEOUT, SECRET_TOOL_TIMEOUT,
                                   FileStore, SecretToolStore, select_store)

TOKEN = "tok_abcdefgh1"


class FakeSecretTool:
    """Mimics `secret-tool` store/lookup/clear on its attribute list. Records (argv, input, timeout) of every call
    so tests can assert no secret hit argv. With hang=True every call times out, like a locked keyring that
    waits for a prompt nobody can answer; hang_on names only the commands that do."""

    def __init__(self, hang: bool = False, hang_on=()) -> None:
        self.items: Dict[Tuple[str, ...], str] = {}
        self.calls: List[Tuple[List[str], Optional[str], Optional[float]]] = []
        self.hang = hang
        self.hang_on = set(hang_on)

    def __call__(self, args, input=None, timeout=None, **_kwargs):
        self.calls.append((list(args), input, timeout))
        if self.hang or args[1] in self.hang_on:
            raise subprocess.TimeoutExpired(args, timeout)
        command = args[1]
        if command == "store":
            self.items[tuple(args[4:])] = input
            return subprocess.CompletedProcess(args, 0, "", "")
        attrs = tuple(args[2:])
        if command == "lookup":
            value = self.items.get(attrs)
            if value is None:
                return subprocess.CompletedProcess(args, 1, "", "")
            return subprocess.CompletedProcess(args, 0, value, "")  # secret-tool prints no newline on a pipe
        if command == "clear":
            self.items.pop(attrs, None)
            return subprocess.CompletedProcess(args, 0, "", "")
        raise AssertionError(f"unexpected command {args}")

    def argv_contains(self, secret: str) -> bool:
        return any(secret in part for args, _, _ in self.calls for part in args)


def found(_name):
    return "/usr/bin/secret-tool"


def missing(_name):
    return None


class FileStoreTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.home = Path(tmp.name)
        self.dir = self.home / ".config" / "moshi-cockpit"
        self.path = self.dir / "secrets.json"
        self.store = FileStore(self.path)

    def test_file_store_round_trip_is_0600_and_atomic(self):
        self.store.write("moshi-device", "device", TOKEN)
        self.assertEqual(self.store.read("moshi-device", "device"), TOKEN)
        self.assertEqual(os.stat(self.path).st_mode & 0o777, 0o600)
        self.assertEqual(os.stat(self.dir).st_mode & 0o777, 0o700)
        self.assertEqual(list(self.dir.glob("*.tmp")), [])
        self.assertTrue(self.store.exists("moshi-device", "device"))
        data = json.loads(self.path.read_text())
        self.assertEqual(data, {"version": 1, "entries": {"moshi-device": {"device": TOKEN}}})

    def test_file_store_delete_and_exists(self):
        self.store.write("moshi-device", "device", TOKEN)
        self.store.write("moshi-vibe", "vibe", "tok_other_value2")
        self.store.delete("moshi-device", "device")
        self.assertFalse(self.store.exists("moshi-device", "device"))
        self.assertEqual(self.store.read("moshi-vibe", "vibe"), "tok_other_value2")
        self.store.delete("moshi-device", "device")  # already gone: no error
        self.store.delete("ocellio-default", "default")

    def test_file_store_refuses_a_file_readable_by_others(self):
        self.store.write("moshi-device", "device", TOKEN)
        os.chmod(self.path, 0o644)
        with self.assertRaises(KeychainError) as caught:
            self.store.read("moshi-device", "device")
        message = str(caught.exception)
        self.assertIn("chmod 600", message)
        self.assertIn("is accessible by other users", message)
        self.assertIn(str(self.path), message)
        self.assertNotIn(TOKEN, message)
        self.assertEqual(self.store.permission_problem(), message)

    def test_permission_problem_is_none_for_a_private_or_missing_file(self):
        self.assertIsNone(self.store.permission_problem())
        self.store.write("moshi-device", "device", TOKEN)
        self.assertIsNone(self.store.permission_problem())

    def test_file_store_never_follows_a_symlink(self):
        self.dir.mkdir(parents=True, mode=0o700)
        target = self.home / "elsewhere.json"
        original = json.dumps({"version": 1, "entries": {"moshi-device": {"device": TOKEN}}})
        target.write_text(original)
        os.chmod(target, 0o600)
        self.path.symlink_to(target)
        with self.assertRaises(KeychainError) as caught:
            self.store.read("moshi-device", "device")
        self.assertIn(str(self.path), str(caught.exception))
        self.assertNotIn(TOKEN, str(caught.exception))
        with self.assertRaises(KeychainError):
            self.store.write("moshi-vibe", "vibe", "tok_other_value2")
        self.assertEqual(target.read_text(), original)
        self.assertTrue(self.path.is_symlink())

    def test_file_store_corrupt_json_names_the_file_not_its_content(self):
        self.dir.mkdir(parents=True, mode=0o700)
        self.path.write_text('{"entries": "tok_secret_x1"')
        os.chmod(self.path, 0o600)
        for call in (lambda: self.store.read("moshi-device", "device"),
                     lambda: self.store.write("moshi-device", "device", TOKEN)):
            with self.assertRaises(KeychainError) as caught:
                call()
            self.assertIn(str(self.path), str(caught.exception))
            self.assertNotIn("tok_secret_x1", str(caught.exception))
        self.assertEqual(self.path.read_text(), '{"entries": "tok_secret_x1"')  # never overwritten

    def test_file_store_parallel_writes_keep_both(self):
        errors = []

        def write(index):
            try:
                FileStore(self.path).write(f"moshi-agent{index}", f"agent{index}", f"tok_parallel_{index}")
            except Exception as error:  # pragma: no cover - shown by the assertion below
                errors.append(error)

        threads = [threading.Thread(target=write, args=(i,)) for i in range(8)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.assertEqual(errors, [])
        for index in range(8):
            self.assertEqual(self.store.read(f"moshi-agent{index}", f"agent{index}"), f"tok_parallel_{index}")

    def test_file_store_rejects_a_bad_token(self):
        with self.assertRaises(KeychainError):
            self.store.write("moshi-device", "device", "a b")
        self.assertFalse(self.path.exists())

    def test_file_store_rejects_a_bad_name(self):
        with self.assertRaises(KeychainError):
            self.store.write("moshi device", "device", TOKEN)
        self.assertFalse(self.path.exists())

    def test_read_of_a_missing_entry_names_the_entry(self):
        with self.assertRaises(KeychainError) as caught:
            self.store.read("moshi-device", "device")
        self.assertIn("moshi-device", str(caught.exception))
        self.assertFalse(self.store.exists("moshi-device", "device"))

    def test_group_bits_alone_are_refused_too(self):
        self.store.write("moshi-device", "device", TOKEN)
        os.chmod(self.path, 0o620)
        self.assertIn("is accessible by other users", self.store.permission_problem())

    def test_an_invalid_token_in_the_file_is_refused_by_read_and_exists(self):
        self.dir.mkdir(parents=True, mode=0o700)
        self.path.write_text(json.dumps({"version": 1, "entries": {"moshi-device": {"device": "a b c"},
                                                                   "moshi-vibe": {"vibe": ""}}}))
        os.chmod(self.path, 0o600)
        for call in (lambda: self.store.read("moshi-device", "device"),
                     lambda: self.store.exists("moshi-vibe", "vibe")):
            with self.assertRaises(KeychainError) as caught:
                call()
            self.assertEqual(str(caught.exception),
                             f"{self.path} is not a valid token file; move it away and connect again")

    def test_an_oversized_or_deeply_nested_file_names_only_the_path(self):
        self.dir.mkdir(parents=True, mode=0o700)
        for content in ("[" * 200000, '{"x": "' + "tok_big_" * 140000 + '"}'):
            self.path.write_text(content)
            os.chmod(self.path, 0o600)
            with self.assertRaises(KeychainError) as caught:
                self.store.read("moshi-device", "device")
            self.assertIn(str(self.path), str(caught.exception))
            self.assertNotIn("tok_big_", str(caught.exception))
            self.assertLess(len(str(caught.exception)), 300)

    def test_an_existing_folder_keeps_its_mode(self):
        self.dir.mkdir(parents=True)
        os.chmod(self.dir, 0o755)
        self.store.write("moshi-device", "device", TOKEN)
        self.assertEqual(os.stat(self.dir).st_mode & 0o777, 0o755)
        self.assertEqual(os.stat(self.path).st_mode & 0o777, 0o600)

    def test_the_folder_is_synced_after_the_rename(self):
        synced = []
        real = os.fsync

        def spy(fd):
            synced.append(stat.S_ISDIR(os.fstat(fd).st_mode))
            real(fd)

        with mock.patch("cockpit.secrets.linux.os.fsync", spy):
            self.store.write("moshi-device", "device", TOKEN)
        self.assertEqual(synced, [False, True])

    def test_os_errors_while_writing_or_locking_are_keychain_errors(self):
        with mock.patch("cockpit.secrets.linux.tempfile.mkstemp", side_effect=PermissionError(13, "Permission denied")):
            with self.assertRaises(KeychainError) as caught:
                self.store.write("moshi-device", "device", TOKEN)
        self.assertIn(str(self.path), str(caught.exception))
        with mock.patch("cockpit.secrets.linux.fcntl.flock", side_effect=OSError(37, "No locks available")):
            with self.assertRaises(KeychainError) as caught:
                self.store.write("moshi-device", "device", TOKEN)
        self.assertIn(".secrets.lock", str(caught.exception))
        self.assertFalse(self.path.exists())

    def test_name_shows_home_as_tilde(self):
        with mock.patch.dict(os.environ, {"HOME": str(self.home)}):
            self.assertEqual(FileStore(self.path).name, "file ~/.config/moshi-cockpit/secrets.json (0600)")


class SecretToolStoreTest(unittest.TestCase):
    ATTRS = ["application", "moshi-cockpit", "service", "moshi-device", "account", "device"]

    def test_secret_tool_store_sends_secret_on_stdin_never_argv(self):
        fake = FakeSecretTool()
        store = SecretToolStore(fake)
        store.write("moshi-device", "device", TOKEN)
        argv, given, _ = fake.calls[-1]
        self.assertEqual(argv, ["secret-tool", "store", "--label", "moshi-cockpit moshi-device"] + self.ATTRS)
        self.assertEqual(given, TOKEN)
        self.assertEqual(store.read("moshi-device", "device"), TOKEN)
        self.assertEqual(fake.calls[-1][0], ["secret-tool", "lookup"] + self.ATTRS)
        self.assertTrue(store.exists("moshi-device", "device"))
        store.delete("moshi-device", "device")
        self.assertEqual(fake.calls[-1][0], ["secret-tool", "clear"] + self.ATTRS)
        self.assertFalse(store.exists("moshi-device", "device"))
        self.assertFalse(fake.argv_contains(TOKEN))
        self.assertEqual(store.name, "Secret Service (libsecret)")
        self.assertEqual(ATTR_APP, ("application", "moshi-cockpit"))

    def test_read_strips_the_output_and_a_missing_entry_is_an_error(self):
        fake = FakeSecretTool()
        fake.items[tuple(self.ATTRS)] = TOKEN + "\n"
        self.assertEqual(SecretToolStore(fake).read("moshi-device", "device"), TOKEN)
        with self.assertRaises(KeychainError) as caught:
            SecretToolStore(fake).read("moshi-vibe", "vibe")
        self.assertIn("moshi-vibe", str(caught.exception))

    def test_secret_tool_timeout_is_a_keychain_error_without_the_value(self):
        fake = FakeSecretTool(hang=True)
        store = SecretToolStore(fake)
        with self.assertRaises(KeychainError) as caught:
            store.read("moshi-device", "device")
        self.assertIn("moshi-device", str(caught.exception))
        self.assertIn("locked", str(caught.exception))
        self.assertIn(NO_ANSWER, str(caught.exception))
        with self.assertRaises(KeychainError) as caught:
            store.write("moshi-device", "device", TOKEN)
        self.assertNotIn(TOKEN, str(caught.exception))
        self.assertIn("locked", str(caught.exception))
        self.assertTrue(fake.calls)
        self.assertTrue(all(timeout == SECRET_TOOL_TIMEOUT for _, _, timeout in fake.calls))

    def test_rejects_a_bad_token_before_calling(self):
        fake = FakeSecretTool()
        with self.assertRaises(KeychainError):
            SecretToolStore(fake).write("moshi-device", "device", "a b")
        self.assertEqual(fake.calls, [])


class SelectStoreTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.home = Path(tmp.name)
        self.path = self.home / ".config" / "moshi-cockpit" / "secrets.json"

    def test_select_prefers_an_existing_file(self):
        FileStore(self.path).write("moshi-device", "device", TOKEN)
        fake = FakeSecretTool()
        store = select_store(self.home, fake, found)
        self.assertIsInstance(store, FileStore)
        self.assertEqual(store.read("moshi-device", "device"), TOKEN)
        self.assertEqual(fake.calls, [])

    def test_select_uses_a_working_secret_tool(self):
        fake = FakeSecretTool()
        store = select_store(self.home, fake, found)
        self.assertIsInstance(store, SecretToolStore)
        probe = ["application", "moshi-cockpit", "service", PROBE_ENTRY[0], "account", PROBE_ENTRY[1]]
        self.assertEqual([args[1] for args, _, _ in fake.calls], ["store", "lookup", "clear"])
        self.assertTrue(all(args[-6:] == probe for args, _, _ in fake.calls))
        self.assertEqual(fake.items, {})  # the probe entry is cleared again
        self.assertFalse(self.path.exists())

    def test_select_falls_back_when_secret_tool_hangs(self):
        fake = FakeSecretTool(hang=True)
        store = select_store(self.home, fake, found)
        self.assertIsInstance(store, FileStore)
        self.assertTrue(fake.calls)
        self.assertTrue(all(timeout == PROBE_TIMEOUT for _, _, timeout in fake.calls))

    def test_probe_clears_its_entry_even_when_the_lookup_hangs(self):
        fake = FakeSecretTool(hang_on={"lookup"})
        self.assertIsInstance(select_store(self.home, fake, found), FileStore)
        self.assertEqual([args[1] for args, _, _ in fake.calls], ["store", "lookup", "clear"])
        self.assertEqual(fake.items, {})

    def test_select_never_falls_back_after_secret_service_was_used(self):
        store = select_store(self.home, FakeSecretTool(), found)
        store.write("moshi-device", "device", TOKEN)
        marker = self.path.parent / MARKER_NAME
        self.assertEqual(marker.read_text(), "secret-service\n")
        self.assertEqual(os.stat(marker).st_mode & 0o777, 0o600)
        self.assertNotIn(TOKEN, marker.read_text())
        for which in (found, missing):
            hanging = FakeSecretTool(hang=True)
            store = select_store(self.home, hanging, which)
            self.assertIsInstance(store, SecretToolStore)
            with self.assertRaises(KeychainError) as caught:
                store.read("moshi-device", "device")
            self.assertIn(NO_ANSWER, str(caught.exception))
            with self.assertRaises(KeychainError):
                store.write("moshi-vibe", "vibe", "tok_other_value2")
        self.assertFalse(self.path.exists())

    def test_no_probe_once_a_store_was_chosen(self):
        select_store(self.home, FakeSecretTool(), found).write("moshi-device", "device", TOKEN)
        fake = FakeSecretTool()
        self.assertIsInstance(select_store(self.home, fake, found), SecretToolStore)
        self.assertEqual(fake.calls, [])
        other = Path(self.home / "other")
        FileStore(other / ".config" / "moshi-cockpit" / "secrets.json").write("moshi-device", "device", TOKEN)
        fake = FakeSecretTool()
        self.assertIsInstance(select_store(other, fake, found), FileStore)
        self.assertEqual(fake.calls, [])

    def test_a_probe_without_a_write_leaves_no_marker(self):
        select_store(self.home, FakeSecretTool(), found)
        self.assertFalse((self.path.parent / MARKER_NAME).exists())

    def test_select_falls_back_when_the_lookup_differs(self):
        fake = FakeSecretTool()
        real = fake.__call__

        def forgetful(args, input=None, timeout=None, **kwargs):
            result = real(args, input=input, timeout=timeout, **kwargs)
            if args[1] == "store":
                fake.items.clear()  # a keyring that accepts and drops
            return result

        self.assertIsInstance(select_store(self.home, forgetful, found), FileStore)

    def test_select_without_secret_tool_is_the_file_store(self):
        fake = FakeSecretTool()
        store = select_store(self.home, fake, missing)
        self.assertIsInstance(store, FileStore)
        self.assertEqual(fake.calls, [])

    def test_default_store_on_linux_and_windows(self):
        store = default_store(system=lambda: "Linux", home=self.home, runner=FakeSecretTool(), which=missing)
        self.assertIsInstance(store, FileStore)
        store = default_store(system=lambda: "Linux", home=self.home, runner=FakeSecretTool(), which=found)
        self.assertIsInstance(store, SecretToolStore)
        with self.assertRaises(UnsupportedPlatform) as caught:
            default_store(system=lambda: "Windows")
        self.assertEqual(str(caught.exception), "Windows is not supported yet")

    def test_paths_headless_from_the_system(self):
        env = {"HOME": str(self.home)}
        self.assertTrue(Paths.from_env(env, system=lambda: "Linux").headless)
        self.assertFalse(Paths.from_env(env, system=lambda: "Darwin").headless)
        self.assertEqual(Paths.from_env(env, system=lambda: "Linux").secrets_file, self.path)


if __name__ == "__main__":
    unittest.main()
