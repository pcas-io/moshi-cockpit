"""`cockpit/hosts.py`: the own host list (hosts.json next to config.json), in a temp HOME."""

import json
import os
import stat
import unittest

from cockpit import hosts
from cockpit.backup import BackupStore
from tests.helpers import temp_paths


class HostsTest(unittest.TestCase):
    def setUp(self):
        self.paths = temp_paths(self)
        self.store = BackupStore(self.paths.state)
        self.file = hosts.hosts_file(self.paths)

    def test_file_sits_next_to_config_json(self):
        self.assertEqual(self.file, self.paths.mesh_config.parent / "hosts.json")
        self.assertEqual(hosts.load_hosts(self.paths), [])  # no file yet

    def test_add_keeps_order_skips_duplicates_and_writes_0600(self):
        self.assertTrue(hosts.add_host(self.paths, self.store, "deploy@build-1"))
        self.assertTrue(hosts.add_host(self.paths, self.store, "devbox:2222"))
        self.assertFalse(hosts.add_host(self.paths, self.store, "deploy@build-1"))
        self.assertEqual(hosts.load_hosts(self.paths), ["deploy@build-1", "devbox:2222"])
        self.assertEqual(json.loads(self.file.read_text()), {"hosts": ["deploy@build-1", "devbox:2222"]})
        self.assertEqual(stat.S_IMODE(os.stat(self.file).st_mode), 0o600)
        self.assertIn(str(self.file), [e["path"] for e in self.store.entries()])  # through the backup store

    def test_remove(self):
        for target in ("devbox", "deploy@build-1", "build-1"):
            hosts.add_host(self.paths, self.store, target)
        self.assertTrue(hosts.remove_host(self.paths, self.store, "deploy@build-1"))
        self.assertFalse(hosts.remove_host(self.paths, self.store, "deploy@build-1"))
        self.assertEqual(hosts.load_hosts(self.paths), ["devbox", "build-1"])
        self.assertEqual(stat.S_IMODE(os.stat(self.file).st_mode), 0o600)

    def test_refuses_what_hc_could_read_as_an_option(self):
        for bad in ("-oProxyCommand=x", "a b", "a;b", "", "$(id)", "a\nb"):
            with self.assertRaises(ValueError):
                hosts.add_host(self.paths, self.store, bad)
            with self.assertRaises(ValueError):
                hosts.remove_host(self.paths, self.store, bad)
        self.assertFalse(self.file.exists())

    def test_load_skips_entries_that_do_not_pass_and_fails_on_a_broken_file(self):
        self.file.parent.mkdir(parents=True, exist_ok=True)
        self.file.write_text(json.dumps({"hosts": ["devbox", "-oX", 7, "devbox", "a b", "build-1"]}))
        os.chmod(self.file, 0o600)
        self.assertEqual(hosts.load_hosts(self.paths), ["devbox", "build-1"])
        for text in ("not json", "[]", '{"hosts": "devbox"}'):
            self.file.write_text(text)
            with self.assertRaises(ValueError):
                hosts.load_hosts(self.paths)
        self.file.write_text('{"hosts": []}')
        os.chmod(self.file, 0o644)
        with self.assertRaises(ValueError):  # owner-only, like config.json
            hosts.load_hosts(self.paths)


if __name__ == "__main__":
    unittest.main()
