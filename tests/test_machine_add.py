"""`machine add --batch`, `machine hosts`, `machine hostkey`, `machine trust` and `saved_hosts` in `machine list`,
with fake runners in a temp HOME (no hc, ssh, ssh-keyscan or ssh-keygen runs)."""

import io
import json
import re
import signal
import subprocess
import unittest
from pathlib import Path
from contextlib import redirect_stderr, redirect_stdout
from unittest import mock

from cockpit import cli, hosts, machines
from cockpit.backup import BackupStore
from tests.helpers import temp_paths
from tests.test_hostkeys import ED, ED_FP, RSA, ssh_g
from tests.test_machines import machine, probe_out


def hc_error(kind, message):
    return json.dumps({"error": {"kind": kind, "message": message}})


class FakeRunner:
    """hc machine ls (from `listings`, the last one repeats), hc machine add (`add`), ssh -G, ssh-keygen -F,
    ssh-keyscan and the probe; records every call."""

    def __init__(self, listings=None, add=(0, "{}", ""), probe=None, known=1, scan=(0, "", "")):
        self.calls, self.listings, self.add = [], list(listings or [[]]), add
        self.probe = probe or (0, probe_out({"claude": "ready"}), "")
        self.known, self.scan = known, scan

    def __call__(self, argv, timeout):
        argv = list(argv)
        self.calls.append((argv, timeout))
        if argv[1:] == ["machine", "ls", "--json"]:
            found = self.listings.pop(0) if len(self.listings) > 1 else self.listings[0]
            return found if isinstance(found, tuple) else (0, json.dumps({"machines": found}), "")
        if argv[1:3] == ["machine", "add"]:
            return self.add
        if argv[0] == "ssh":
            return 0, ssh_g(), ""
        if argv[0] == "ssh-keygen":
            return self.known, "", ""
        if argv[0] == "ssh-keyscan":
            return self.scan
        return self.probe

    def adds(self):
        return [(argv, timeout) for argv, timeout in self.calls if argv[1:3] == ["machine", "add"]]


class AddBatchTest(unittest.TestCase):
    def setUp(self):
        self.paths = temp_paths(self)
        self.store = BackupStore(self.paths.state)
        self.hc = str(self.paths.herdr_core_bin)

    def add(self, target, runner):
        return machines.add_batch(self.paths, self.store, target, runner)

    def test_success_saves_the_target_and_returns_the_inventory(self):
        runner = FakeRunner(listings=[[machine("devbox")], [machine("devbox"), machine("build-1", target="deploy@build-1")]],
                            add=(0, json.dumps({"name": "build-1"}), ""))
        answer = self.add("deploy@build-1", runner)
        self.assertEqual(runner.adds(), [([self.hc, "machine", "add", "--install", "--json", "--", "deploy@build-1"],
                                          machines.ADD_TIMEOUT)])
        self.assertEqual(machines.ADD_TIMEOUT, 120)
        self.assertEqual((answer["ok"], answer["machine"]), (True, "build-1"))
        self.assertEqual(answer["inventory"]["machine"], "build-1")
        self.assertEqual(answer["inventory"]["harnesses"][0]["state"], "ready")
        self.assertFalse(answer["inventory"]["cached"])
        self.assertEqual(hosts.load_hosts(self.paths), ["deploy@build-1"])

    def test_success_finds_the_machine_in_the_list_when_hc_does_not_name_it(self):
        runner = FakeRunner(listings=[[], [machine("box", target="me@box")]], add=(0, "", ""))
        answer = self.add("me@box", runner)
        self.assertEqual((answer["ok"], answer["machine"]), (True, "box"))
        runner = FakeRunner(listings=[[]], add=(0, "", ""))
        answer = self.add("me@box", runner)
        self.assertEqual(answer, {"ok": True, "machine": None, "inventory": None,
                                  "message": "me@box was added; Herdr does not list it yet."})

    def test_success_with_a_machine_that_does_not_answer_yet(self):
        runner = FakeRunner(listings=[[], [machine("devbox")]], add=(0, '{"name": "devbox"}', ""),
                            probe=(255, "", "ssh: no route"))
        answer = self.add("devbox", runner)
        self.assertEqual((answer["ok"], answer["machine"], answer["inventory"]), (True, "devbox", None))
        self.assertTrue(answer["message"].startswith("devbox was added; devbox did not answer: "), answer)

    def test_an_ssh_config_alias_is_not_saved(self):
        (self.paths.home / ".ssh").mkdir()
        (self.paths.home / ".ssh" / "config").write_text("Host devbox\n  HostName build-1\n")
        self.add("devbox", FakeRunner(add=(1, hc_error("unreachable", "no route"), "")))
        self.assertEqual(hosts.load_hosts(self.paths), [])
        self.add("deploy@build-1", FakeRunner(add=(1, hc_error("unreachable", "no route"), "")))
        self.assertEqual(hosts.load_hosts(self.paths), ["deploy@build-1"])

    def test_each_hc_error_maps_to_one_answer(self):
        cases = [
            ((1, hc_error("needs_login", "logging in needs a terminal"), ""), "tab"),
            ((1, hc_error("login_needed", "a person types the password or code"), ""), "password"),
            ((1, hc_error("auth_failed", "ssh refused"), ""), "tab"),
            ((1, "", hc_error("unreachable", "ssh said why, above") + "\nme@build-1: Permission denied "
                                                                        "(publickey,password).\n"), "password"),
            ((1, "", "(keyboard-interactive) verification code:\n"), "password"),
            ((1, hc_error("unreachable", "ssh said why, above"), "me@build-1: Permission denied (publickey).\n"), "key"),
            ((1, hc_error("host_key", "the host key is not known"), ""), "host_key"),
            ((1, hc_error("unreachable", "ssh said why, above"), "Host key verification failed.\n"), "host_key"),
            ((1, hc_error("unreachable", "no route to host"), ""), None),
            ((1, hc_error("install_failed", "nothing was changed there"), ""), None),
            ((1, "not json", "error: core gone\nmore"), None),
            ((124, "", "no answer within 120 s"), None),
        ]
        for reply, needs in cases:
            answer = self.add("me@build-1", FakeRunner(add=reply))
            self.assertIs(answer["ok"], False, reply)
            self.assertEqual(answer.get("needs"), needs, reply)
            self.assertTrue(answer["message"], reply)
            self.assertNotIn("\n", answer["message"], reply)
        answer = self.add("me@build-1", FakeRunner(add=(1, hc_error("unreachable", "no route to host"), "")))
        self.assertEqual(answer, {"ok": False, "message": "no route to host"})
        answer = self.add("me@build-1", FakeRunner(add=(1, "not json", "error: core gone\nmore")))
        self.assertEqual(answer, {"ok": False, "message": "error: core gone"})
        answer = self.add("me@build-1", FakeRunner(add=(255, "", "me@build-1: Permission denied (publickey).\n")))
        self.assertEqual(answer, {"ok": False, "needs": "key",
                                  "message": "me@build-1 accepts only ssh keys and none of yours worked."})

    def test_a_broken_hosts_file_does_not_block_the_add(self):
        file = hosts.hosts_file(self.paths)
        file.parent.mkdir(parents=True, exist_ok=True)
        file.write_text("not json")
        runner = FakeRunner(listings=[[], [machine("build-1")]], add=(0, '{"name": "build-1"}', ""))
        answer = self.add("deploy@build-1", runner)
        self.assertEqual((answer["ok"], answer["saved"], answer["machine"]), (True, False, "build-1"))
        self.assertEqual(len(runner.adds()), 1)
        self.assertEqual(file.read_text(), "not json")  # left as it was
        answer = self.add("deploy@build-1", FakeRunner(add=(1, hc_error("unreachable", "no route"), "")))
        self.assertEqual(answer, {"ok": False, "saved": False, "message": "no route"})

    def test_worst_case_stays_under_the_mods_exec_timeout(self):
        self.assertEqual(machines.ADD_WORST, machines.LIST_TIMEOUT * 2 + machines.ADD_TIMEOUT + machines.PROBE_TIMEOUT)
        self.assertEqual(machines.ADD_WORST, 195)
        self.assertLessEqual(machines.ADD_WORST, 200)
        source = (Path(machines.__file__).parent.parent / "share/herdr-mods/mesh-agents/machines.js").read_text()
        exec_ms = int(re.search(r"ADD_EXEC = \{ timeout: (\d+) \}", source).group(1))
        self.assertGreater(exec_ms, machines.ADD_WORST * 1000)


    def test_refuses_an_option_before_anything_runs(self):
        for bad in ("-oProxyCommand=x", "a b", "$(id)", ""):
            runner = FakeRunner()
            with self.assertRaises(ValueError):
                self.add(bad, runner)
            self.assertEqual(runner.calls, [])
        self.assertEqual(hosts.load_hosts(self.paths), [])

    def test_list_includes_saved_hosts_minus_known_machines(self):
        (self.paths.home / ".ssh").mkdir()
        (self.paths.home / ".ssh" / "config").write_text("Host lab-2\n")
        for target in ("deploy@build-1", "me@devbox:2222", "build-2", "lab-2"):
            hosts.add_host(self.paths, self.store, target)
        runner = FakeRunner(listings=[[machine("build-1", target="deploy@build-1")]])
        found = machines.list_machines(self.paths, runner)
        self.assertEqual(found["ssh_hosts"], ["lab-2"])
        self.assertEqual(found["saved_hosts"], ["me@devbox:2222", "build-2"])

    def test_list_survives_a_broken_hosts_file(self):
        file = hosts.hosts_file(self.paths)
        file.parent.mkdir(parents=True, exist_ok=True)
        file.write_text("not json")
        found = machines.list_machines(self.paths, FakeRunner(listings=[[machine("devbox")]]))
        self.assertEqual((found["saved_hosts"], len(found["machines"])), ([], 1))
        self.assertIn("hosts.json", found["problem"])


class RunBatchTest(unittest.TestCase):
    def test_a_timeout_kills_the_whole_process_group(self):
        proc = mock.Mock(pid=4242)
        proc.communicate.side_effect = [subprocess.TimeoutExpired("hc", 120), ("", "")]
        with mock.patch("cockpit.machines.subprocess.Popen", return_value=proc) as popen, \
                mock.patch("cockpit.machines.os.killpg") as killpg:
            self.assertEqual(machines.run_batch(["hc", "machine", "add"], 120), (124, "", "no answer within 120 s"))
        self.assertTrue(popen.call_args.kwargs["start_new_session"])
        self.assertIs(popen.call_args.kwargs["stdin"], subprocess.DEVNULL)
        killpg.assert_called_once_with(4242, signal.SIGKILL)
        self.assertEqual(proc.communicate.call_args_list[0], mock.call(timeout=120))

    def test_an_answer_and_a_missing_program(self):
        proc = mock.Mock(pid=1, returncode=1)
        proc.communicate.return_value = ("out", "err")
        with mock.patch("cockpit.machines.subprocess.Popen", return_value=proc), \
                mock.patch("cockpit.machines.os.killpg") as killpg:
            self.assertEqual(machines.run_batch(["hc"], 120), (1, "out", "err"))
        killpg.assert_not_called()
        with mock.patch("cockpit.machines.subprocess.Popen", side_effect=FileNotFoundError(2, "No such file")):
            self.assertEqual(machines.run_batch(["hc"], 120), (127, "", "No such file"))

class MachineAddCliTest(unittest.TestCase):
    def setUp(self):
        self.paths = temp_paths(self)

    def run_cli(self, argv, runner):
        out, err = io.StringIO(), io.StringIO()
        with mock.patch("cockpit.cli.Paths.from_env", return_value=self.paths), \
                mock.patch("cockpit.machines.run_capture", runner), mock.patch("cockpit.machines.run_batch", runner), \
                mock.patch("cockpit.cli._on_terminal", return_value=False), redirect_stdout(out), redirect_stderr(err):
            code = cli.main(argv)
        return code, out.getvalue(), err.getvalue()

    def test_add_batch_prints_structured_answers_with_exit_0(self):
        runner = FakeRunner(listings=[[], [machine("devbox")]], add=(0, '{"name": "devbox"}', ""))
        code, out, _ = self.run_cli(["machine", "add", "me@devbox", "--batch", "--json"], runner)
        self.assertEqual(code, 0)
        data = json.loads(out)
        self.assertEqual((data["ok"], data["machine"], len(data["inventory"]["harnesses"])), (True, "devbox", 6))
        code, out, _ = self.run_cli(["machine", "add", "me@devbox", "--batch", "--json"],
                                    FakeRunner(add=(1, hc_error("needs_login", "logging in needs a terminal"), "")))
        self.assertEqual((code, json.loads(out)), (0, {"ok": False, "needs": "tab",
                                                       "message": "logging in needs a terminal"}))
        code, out, err = self.run_cli(["machine", "add", "--batch", "--json", "--", "-oProxyCommand=x"], FakeRunner())
        self.assertEqual((code, out), (1, ""))
        self.assertEqual(err.count("\n"), 1)

    def test_add_without_batch_stays_the_attached_tab_command(self):
        with mock.patch("cockpit.harness_install.run_argv", return_value=0) as call:
            code, _, _ = self.run_cli(["machine", "add", "devbox"], FakeRunner(listings=[[], [machine("devbox")]]))
        self.assertEqual(code, 0)
        call.assert_called_once_with([str(self.paths.herdr_core_bin), "machine", "add", "--", "devbox"])
        self.assertEqual(hosts.load_hosts(self.paths), [])

    def test_hosts_add_and_remove(self):
        code, out, _ = self.run_cli(["machine", "hosts", "add", "deploy@build-1"], FakeRunner())
        self.assertEqual((code, json.loads(out)), (0, {"ok": True}))
        self.assertEqual(hosts.load_hosts(self.paths), ["deploy@build-1"])
        code, out, _ = self.run_cli(["machine", "hosts", "remove", "deploy@build-1"], FakeRunner())
        self.assertEqual((code, json.loads(out)), (0, {"ok": True}))
        self.assertEqual(hosts.load_hosts(self.paths), [])
        code, out, err = self.run_cli(["machine", "hosts", "add", "a;b"], FakeRunner())
        self.assertEqual((code, out, err.count("\n")), (1, "", 1))

    def test_hostkey_json(self):
        runner = FakeRunner(known=1, scan=(0, f"build-1 ssh-rsa {RSA}\nbuild-1 ssh-ed25519 {ED}\n", ""))
        code, out, _ = self.run_cli(["machine", "hostkey", "deploy@build-1", "--json"], runner)
        self.assertEqual(code, 0)
        data = json.loads(out)
        self.assertEqual(set(data), {"target", "host", "port", "known", "keys"})
        self.assertEqual((data["known"], data["keys"][0]), (False, {"type": "ssh-ed25519", "sha256": ED_FP}))
        code, out, err = self.run_cli(["machine", "hostkey", "devbox", "--json"],
                                      FakeRunner(known=1, scan=(1, "", "build-1: Connection refused\n")))
        self.assertEqual((code, out), (1, ""))
        self.assertIn("devbox did not answer: build-1: Connection refused", err)

    def test_trust_json(self):
        runner = FakeRunner(scan=(0, f"build-1 ssh-ed25519 {ED}\n", ""))
        code, out, _ = self.run_cli(["machine", "trust", "devbox", "--type", "ssh-ed25519", "--fingerprint", ED_FP,
                                     "--json"], runner)
        self.assertEqual((code, json.loads(out)), (0, {"ok": True}))
        known_hosts = self.paths.home / ".ssh" / "known_hosts"
        self.assertEqual(known_hosts.read_text(), f"build-1 ssh-ed25519 {ED}\n")
        code, out, err = self.run_cli(["machine", "trust", "devbox", "--type", "ssh-rsa", "--fingerprint", ED_FP,
                                       "--json"], runner)
        self.assertEqual((code, out), (1, ""))
        self.assertIn("The host key changed since you looked at it; nothing was trusted", err)
        self.assertEqual(known_hosts.read_text(), f"build-1 ssh-ed25519 {ED}\n")


if __name__ == "__main__":
    unittest.main()
