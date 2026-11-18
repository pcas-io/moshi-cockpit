"""Host aliases from ~/.ssh/config: order, Include handling, skipped patterns, read-only."""

import os
import tempfile
import unittest
from pathlib import Path

from cockpit.ssh_config import read_hosts


class SshConfigTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.home = Path(tmp.name)
        self.ssh = self.home / ".ssh"
        self.ssh.mkdir()

    def write(self, name, text):
        path = self.ssh / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
        return path

    def test_plain_hosts_in_order_and_unique(self):
        self.write("config", "Host devbox build-1\n  HostName x\nHost devbox\n# Host nope\n\nHOST=lab-2\nhost \"quoted\"\n")
        self.assertEqual(read_hosts(self.home), ["devbox", "build-1", "lab-2", "quoted"])

    def test_wildcards_negations_and_match_blocks_are_skipped(self):
        self.write("extra", "Host from-extra\n")
        self.write("config", "Host *\nHost !devbox\nHost web?\nHost devbox\n"
                             "Match host x\n  Include extra\n  Host inside\nHost build-1\n")
        self.assertEqual(read_hosts(self.home), ["devbox", "inside", "build-1"])

    def test_match_block_ends_at_next_host(self):
        self.write("extra", "Host from-extra\n")
        self.write("config", "Match host x\n  Include extra\nHost build-1\n")
        self.assertEqual(read_hosts(self.home), ["build-1"])

    def test_include_relative_absolute_and_glob(self):
        self.write("conf.d/a", "Host devbox\n")
        self.write("conf.d/b", "Host build-1\n")
        self.write("abs", "Host lab-2\n")
        self.write("config", "Include conf.d/*\nInclude %s\nInclude ~/.ssh/tilde\n" % (self.ssh / "abs"))
        self.write("tilde", "Host tilde-1\n")
        self.assertEqual(read_hosts(self.home), ["devbox", "build-1", "lab-2", "tilde-1"])

    def test_include_cycles_globs_and_unreadable_files_are_survived(self):
        self.write("a", "Host devbox\nInclude b\n")
        self.write("b", "Host build-1\nInclude a\nInclude missing\nInclude nothing/*\n")
        self.write("config", "Include a\nInclude config\nHost lab-2\n")
        self.assertEqual(read_hosts(self.home), ["devbox", "build-1", "lab-2"])

    @unittest.skipIf(hasattr(os, "geteuid") and os.geteuid() == 0, "mode 000 is readable as root")
    def test_a_mode_000_file_is_skipped(self):
        locked = self.write("locked", "Host hidden\n")
        os.chmod(locked, 0)
        self.addCleanup(os.chmod, locked, 0o600)
        self.write("config", "Include locked\nHost lab-2\n")
        self.assertEqual(read_hosts(self.home), ["lab-2"])

    def test_depth_limit(self):
        for i in range(30):
            self.write("c%d" % i, "Host h%d\nInclude c%d\n" % (i, i + 1))
        self.write("config", "Include c0\n")
        hosts = read_hosts(self.home)
        self.assertLessEqual(len(hosts), 17)
        self.assertEqual(hosts[0], "h0")

    def test_no_config_gives_an_empty_list(self):
        self.assertEqual(read_hosts(self.home), [])

    def test_the_files_are_never_written(self):
        path = self.write("config", "Host devbox\nInclude more\n")
        more = self.write("more", "Host build-1\n")
        before = [(p.read_bytes(), p.stat().st_mtime_ns) for p in (path, more)]
        read_hosts(self.home)
        after = [(p.read_bytes(), p.stat().st_mtime_ns) for p in (path, more)]
        self.assertEqual(before, after)
        self.assertEqual(sorted(p.name for p in self.ssh.iterdir()), ["config", "more"])


if __name__ == "__main__":
    unittest.main()
