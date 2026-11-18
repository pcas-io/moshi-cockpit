"""`cockpit/hostkeys.py`: targets, host keys and known_hosts, with a fake runner and a temp HOME (no ssh,
ssh-keyscan or ssh-keygen -F runs; only `ssh-keygen -t/-l` on temp files for the fingerprint check)."""

import os
import shutil
import stat
import subprocess
import tempfile
import unittest
from pathlib import Path

from cockpit import hostkeys
from tests.helpers import temp_paths

ED = "AAAAC3NzaC1lZDI1NTE5AAAAIBlZpY++q9PhySGbVev498eY238NlGlpMYbrx3sF1wEc"
ED_FP = "SHA256:AlQFAzJ5rO26Lgs7fs+OsYg0HFG32J+2ZmesKFP0184"
OTHER = "AAAAC3NzaC1lZDI1NTE5AAAAIGT53UunudF1a1gcRfwXa0STowXhqGjCTetFG5Z6Erd7"
ECDSA = ("AAAAE2VjZHNhLXNoYTItbmlzdHAyNTYAAAAIbmlzdHAyNTYAAABBBBnDo7U/FM0RMD/Woo2lhk7pzJOpws3rS5xdA0SukkCdXyTu5iyLK"
         "DhgNVDYNDjHRPQSFfdecuRE7BUyyGsYqPo=")
RSA = "AAAAB3NzaC1yc2EAAAADAQABAAAAgQC7"


DEFAULTS = ("userknownhostsfile ~/.ssh/known_hosts ~/.ssh/known_hosts2\n"
            "globalknownhostsfile /etc/ssh/ssh_known_hosts /etc/ssh/ssh_known_hosts2\n")


def ssh_g(hostname="build-1", port=22, user="me"):
    return f"user {user}\nhostname {hostname}\nport {port}\nstricthostkeychecking ask\n"


class FakeRunner:
    """Answers ssh -G, ssh-keygen -F and ssh-keyscan; records every call."""

    def __init__(self, resolve=None, known=1, scans=None):
        self.calls = []
        self.resolve = resolve if resolve is not None else (0, ssh_g(), "")
        self.known = known
        self.scans = list(scans if scans is not None else [(0, "", "")])

    def __call__(self, argv, timeout):
        self.calls.append((list(argv), timeout))
        if argv[0] == "ssh":
            return self.resolve
        if argv[0] == "ssh-keygen":
            return self.known, "", ""
        if argv[0] == "ssh-keyscan":
            return self.scans.pop(0) if len(self.scans) > 1 else self.scans[0]
        raise AssertionError(f"unexpected command {argv}")


class TargetTest(unittest.TestCase):
    def test_split_target(self):
        cases = {"devbox": (None, "devbox", None), "deploy@build-1": ("deploy", "build-1", None),
                 "deploy@build-1:2222": ("deploy", "build-1", 2222), "build-1:2222": (None, "build-1", 2222),
                 "me@10.0.0.7": ("me", "10.0.0.7", None), "build-1.lan": (None, "build-1.lan", None)}
        for target, parts in cases.items():
            self.assertEqual(hostkeys.split_target(target), parts, target)

    def test_split_target_refuses(self):
        for bad in ("-oProxyCommand=x", "a b", "a;b", "", "$(id)", "a\nb", "@build-1", "deploy@", "a@b@c",
                    "build-1:", "build-1:0", "build-1:65536", "build-1:22a", "build-1:2:3", "me@:22", None):
            with self.assertRaises(ValueError, msg=repr(bad)):
                hostkeys.split_target(bad)

    def setUp(self):
        self.home = temp_paths(self).home

    def test_resolve_asks_ssh_for_the_real_host_and_port(self):
        runner = FakeRunner(resolve=(0, ssh_g("10.0.0.7", 2200, "deploy"), ""))
        self.assertEqual(hostkeys.resolve("devbox", runner, self.home), ("deploy", "10.0.0.7", 2200))
        self.assertEqual(runner.calls, [(["ssh", "-G", "devbox"], hostkeys.LOCAL_TIMEOUT)])
        runner = FakeRunner(resolve=(0, ssh_g("build-1", 2222, "deploy"), ""))
        self.assertEqual(hostkeys.resolve("deploy@build-1:2222", runner, self.home), ("deploy", "build-1", 2222))
        self.assertEqual(runner.calls[0][0], ["ssh", "-G", "-p", "2222", "deploy@build-1"])

    def test_resolve_refuses_odd_answers(self):
        for reply in [(255, "", "bad config\nmore"), (0, "port 22\n", ""), (0, ssh_g("-oX"), ""),
                      (0, ssh_g(port="x"), ""), (0, ssh_g("a b"), "")]:
            with self.assertRaises(ValueError, msg=repr(reply)):
                hostkeys.resolve("devbox", FakeRunner(resolve=reply), self.home)
        with self.assertRaises(ValueError):
            hostkeys.resolve("-oProxyCommand=x", FakeRunner(), self.home)

    def test_defaults_ssh_prints_are_handled(self):
        for extra in ["", "proxycommand none\nproxyjump none\nhashknownhosts no\n",
                      DEFAULTS, DEFAULTS.replace("~", str(self.home))]:
            reply = (0, ssh_g() + extra, "")
            self.assertEqual(hostkeys.resolve("devbox", FakeRunner(resolve=reply), self.home), ("me", "build-1", 22))

    def test_settings_the_dialog_cannot_follow_send_the_host_to_a_tab(self):
        cases = {"proxyjump jump-1\n": "ProxyJump", "proxycommand ssh -W %h:%p jump-1\n": "ProxyCommand",
                 "hostkeyalias build-1-key\n": "HostKeyAlias",
                 "userknownhostsfile ~/.ssh/other_hosts\n": "UserKnownHostsFile",
                 "userknownhostsfile /dev/null\n": "UserKnownHostsFile",
                 "globalknownhostsfile /etc/ssh/ssh_known_hosts\n": "GlobalKnownHostsFile",
                 "hashknownhosts yes\n": "HashKnownHosts"}
        for extra, name in cases.items():
            runner = FakeRunner(resolve=(0, ssh_g() + extra, ""))
            with self.assertRaises(hostkeys.NeedsTab) as caught:
                hostkeys.resolve("devbox", runner, self.home)
            self.assertEqual(str(caught.exception),
                             f"devbox uses ssh settings this dialog doesn't handle ({name}); add it in a tab.")
            answer = hostkeys.hostkey("devbox", runner, self.home)
            self.assertEqual(answer, {"target": "devbox", "needs": "tab", "message": str(caught.exception)})
            self.assertEqual({c[0][0] for c in runner.calls}, {"ssh"})  # no ssh-keygen, no ssh-keyscan
        runner = FakeRunner(resolve=(0, ssh_g() + "proxyjump jump-1\nhostkeyalias k\nhashknownhosts yes\n", ""))
        with self.assertRaises(hostkeys.NeedsTab) as caught:
            hostkeys.resolve("devbox", runner, self.home)
        self.assertIn("(ProxyJump, HostKeyAlias, HashKnownHosts)", str(caught.exception))


class FingerprintTest(unittest.TestCase):
    def test_matches_ssh_keygen(self):
        tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, tmp)
        key = os.path.join(tmp, "key")
        subprocess.run(["ssh-keygen", "-q", "-t", "ed25519", "-f", key, "-N", "", "-C", "test"], check=True,
                       stdin=subprocess.DEVNULL)
        blob = Path(key + ".pub").read_text().split()[1]
        listed = subprocess.run(["ssh-keygen", "-lf", key + ".pub"], check=True, capture_output=True, text=True)
        self.assertEqual(hostkeys.fingerprint(blob), listed.stdout.split()[1])
        self.assertEqual(hostkeys.fingerprint(ED), ED_FP)

    def test_refuses_what_is_not_base64(self):
        for bad in ("not base64!", "", "AAAA$"):
            with self.assertRaises(ValueError):
                hostkeys.fingerprint(bad)


class HostKeyTest(unittest.TestCase):
    def setUp(self):
        self.paths = temp_paths(self)
        self.home = self.paths.home
        self.known_hosts = self.home / ".ssh" / "known_hosts"

    def test_known_asks_ssh_keygen_about_our_known_hosts(self):
        runner = FakeRunner(known=0)
        self.assertTrue(hostkeys.known("build-1", 22, runner, self.home))
        self.assertFalse(hostkeys.known("build-1", 2222, FakeRunner(known=1), self.home))
        self.assertEqual(runner.calls, [(["ssh-keygen", "-F", "build-1", "-f", str(self.known_hosts)],
                                         hostkeys.LOCAL_TIMEOUT)])
        runner = FakeRunner(known=0)
        hostkeys.known("build-1", 2222, runner, self.home)
        self.assertEqual(runner.calls[0][0][:3], ["ssh-keygen", "-F", "[build-1]:2222"])

    def test_scan_orders_keys_and_ignores_comments(self):
        out = (f"# build-1:22 SSH-2.0-OpenSSH_9.6\nbuild-1 ssh-rsa {RSA}\nbuild-1 ecdsa-sha2-nistp256 {ECDSA}\n"
               f"build-1 ssh-ed25519 {ED}\nother ssh-ed25519 {OTHER}\nbuild-1 ssh-ed25519\n\n")
        runner = FakeRunner(scans=[(0, out, "")])
        keys = hostkeys.scan("build-1", 22, runner)
        self.assertEqual(runner.calls, [(["ssh-keyscan", "-T", "8", "build-1"], hostkeys.SCAN_TIMEOUT)])
        self.assertEqual([k.type for k in keys], ["ssh-ed25519", "ecdsa-sha2-nistp256", "ssh-rsa"])
        self.assertEqual(keys[0], hostkeys.Key("ssh-ed25519", f"build-1 ssh-ed25519 {ED}", ED_FP))
        runner = FakeRunner(scans=[(0, f"[build-1]:2222 ssh-ed25519 {ED}\nbuild-1 ssh-ed25519 {OTHER}\n", "")])
        keys = hostkeys.scan("build-1", 2222, runner)
        self.assertEqual(runner.calls[0][0], ["ssh-keyscan", "-T", "8", "-p", "2222", "build-1"])
        self.assertEqual([k.line for k in keys], [f"[build-1]:2222 ssh-ed25519 {ED}"])  # host field checked

    def test_hostkey_answer(self):
        scan = (0, f"build-1 ssh-rsa {RSA}\nbuild-1 ssh-ed25519 {ED}\n", "")
        answer = hostkeys.hostkey("deploy@build-1", FakeRunner(known=1, scans=[scan]), self.home)
        self.assertEqual(answer, {"target": "deploy@build-1", "host": "build-1", "port": 22, "known": False,
                                  "keys": [{"type": "ssh-ed25519", "sha256": ED_FP},
                                           {"type": "ssh-rsa", "sha256": hostkeys.fingerprint(RSA)}]})
        runner = FakeRunner(known=0, scans=[scan])
        self.assertEqual(hostkeys.hostkey("devbox", runner, self.home)["known"], True)
        self.assertEqual([c[0][0] for c in runner.calls], ["ssh", "ssh-keygen"])  # known: no scan

    def test_hostkey_of_a_machine_that_does_not_answer(self):
        for reply in [(1, "", "# build-1:22 SSH-2.0\nbuild-1: Connection refused\n"), (0, "", ""), (124, "", "")]:
            with self.assertRaises(ValueError) as caught:
                hostkeys.hostkey("devbox", FakeRunner(known=1, scans=[reply]), self.home)
            self.assertTrue(str(caught.exception).startswith("devbox did not answer: "), caught.exception)
            self.assertNotIn("\n", str(caught.exception))
        with self.assertRaises(ValueError) as caught:
            hostkeys.hostkey("devbox", FakeRunner(known=1, scans=[(1, "", "build-1: Connection refused\n")]),
                             self.home)
        self.assertEqual(str(caught.exception), "devbox did not answer: build-1: Connection refused")

    def test_trust_creates_ssh_folder_and_file_with_modes(self):
        runner = FakeRunner(scans=[(0, f"build-1 ssh-ed25519 {ED}\n", "")])
        hostkeys.trust("build-1", 22, "ssh-ed25519", ED_FP, runner, self.home)
        self.assertEqual(self.known_hosts.read_text(), f"build-1 ssh-ed25519 {ED}\n")
        self.assertEqual(stat.S_IMODE(os.stat(self.known_hosts.parent).st_mode), 0o700)
        self.assertEqual(stat.S_IMODE(os.stat(self.known_hosts).st_mode), 0o600)
        self.assertEqual(runner.calls, [(["ssh-keyscan", "-T", "8", "build-1"], hostkeys.SCAN_TIMEOUT)])

    def test_trust_appends_exactly_one_line_and_keeps_the_others(self):
        self.known_hosts.parent.mkdir(mode=0o700)
        self.known_hosts.write_text("devbox ssh-ed25519 AAAA1\n|1|hashed= ssh-rsa AAAA2")  # no final newline
        os.chmod(self.known_hosts, 0o644)
        scan = (0, f"[build-1]:2222 ssh-rsa {RSA}\n[build-1]:2222 ssh-ed25519 {ED}\n", "")
        hostkeys.trust("build-1", 2222, "ssh-ed25519", ED_FP, FakeRunner(scans=[scan]), self.home)
        self.assertEqual(self.known_hosts.read_text(), "devbox ssh-ed25519 AAAA1\n|1|hashed= ssh-rsa AAAA2\n"
                                                       f"[build-1]:2222 ssh-ed25519 {ED}\n")
        self.assertEqual(stat.S_IMODE(os.stat(self.known_hosts).st_mode), 0o644)  # an existing file keeps its mode

    def test_trust_refuses_a_changed_key(self):
        self.known_hosts.parent.mkdir(mode=0o700)
        self.known_hosts.write_text("devbox ssh-ed25519 AAAA1\n")
        for scan in [(0, f"build-1 ssh-ed25519 {OTHER}\n", ""), (0, f"build-1 ssh-rsa {RSA}\n", "")]:
            with self.assertRaises(ValueError) as caught:
                hostkeys.trust("build-1", 22, "ssh-ed25519", ED_FP, FakeRunner(scans=[scan]), self.home)
            self.assertEqual(str(caught.exception), "The host key changed since you looked at it; nothing was trusted")
        self.assertEqual(self.known_hosts.read_text(), "devbox ssh-ed25519 AAAA1\n")

    def test_trust_of_a_machine_that_does_not_answer(self):
        for scan, why in [((1, "", "build-1: Connection refused\n"), "build-1: Connection refused"),
                          ((0, "", ""), "no host key"), ((0, f"other ssh-ed25519 {ED}\n", ""), "no host key")]:
            with self.assertRaises(ValueError) as caught:
                hostkeys.trust("build-1", 22, "ssh-ed25519", ED_FP, FakeRunner(scans=[scan]), self.home, "devbox")
            self.assertEqual(str(caught.exception), f"devbox did not answer: {why}")
        self.assertFalse(self.known_hosts.exists())

    def test_trust_refuses_odd_arguments_before_scanning(self):
        for key_type, fp in [("ssh ed25519", ED_FP), ("ssh-ed25519", "MD5:aa"), ("ssh-ed25519", ED_FP + "\n"),
                             ("-x", ED_FP)]:
            runner = FakeRunner()
            with self.assertRaises(ValueError):
                hostkeys.trust("build-1", 22, key_type, fp, runner, self.home)
            self.assertEqual(runner.calls, [])
        self.assertFalse(self.known_hosts.exists())


if __name__ == "__main__":
    unittest.main()
