"""`machine list | inventory | add`: Herdr's machines, ssh hosts and the agent CLIs on each, with fake runners
(nothing real runs: no hc, no ssh, no /bin/sh)."""

import io
import json
import os
import stat
import threading
import unittest
from contextlib import redirect_stderr, redirect_stdout
from unittest import mock

from cockpit import cli, machines
from cockpit.harness_install import INSTALL, shown_command
from cockpit.inventory import KNOWN, LABELS, MARK, PATH_EXTRA, STATE_TEXT, held_argv, probe_script
from tests.helpers import temp_paths


def probe_out(states=None, curl=True, noise=True):
    """What the probe prints: one marker line per harness, MOTD noise around it."""
    states = states or {}
    lines = ["Welcome to devbox"] if noise else []
    for name in KNOWN:
        state = states.get(name, "missing")
        installed, signed = {"ready": ("1", "1"), "installed": ("1", "0"), "missing": ("0", "0")}[state]
        lines.append("\t".join((MARK, name, installed, signed)))
    lines.append("\t".join((MARK, "need", "curl", "1" if curl else "0")))
    return "\n".join(lines) + "\n"


def machine(name, state="connected", target=None, temporary=False, host=None):
    return {"host": host or name, "id": "m-" + name, "keeps_running_after_logout": None, "logout_linger_user": None,
            "name": name, "session": None, "state": state, "target": target or f"me@{name}",
            "temporary": temporary, "version": "0.1.21", "via": False}


class FakeRunner:
    """Records (argv, timeout); answers `hc machine ls --json` from `listings` (one per call, the last one
    repeats) and every other command with `probe`."""

    def __init__(self, probe=(0, "", ""), listings=None):
        self.calls, self.probe, self.listings = [], probe, list(listings or [[]])

    def __call__(self, argv, timeout):
        self.calls.append((list(argv), timeout))
        if list(argv[1:]) == ["machine", "ls", "--json"]:
            found = self.listings.pop(0) if len(self.listings) > 1 else self.listings[0]
            if isinstance(found, tuple):
                return found
            return 0, json.dumps({"machines": found}), ""
        return self.probe

    @property
    def probes(self):
        return [argv for argv, _ in self.calls if argv[1:4] != ["machine", "ls", "--json"]]


class MachinesTest(unittest.TestCase):
    def setUp(self):
        self.paths = temp_paths(self)
        self.hc = str(self.paths.herdr_core_bin)
        self.cache = self.paths.state / "inventory.json"

    def ssh_config(self, text):
        (self.paths.home / ".ssh").mkdir(exist_ok=True)
        (self.paths.home / ".ssh" / "config").write_text(text)

    def test_list_merges_herdr_machines_and_unknown_ssh_hosts(self):
        self.ssh_config("Host devbox\n  User me\nHost lab-2\n")
        runner = FakeRunner(listings=[[machine("devbox"), machine("build-1", "asleep"),
                                       machine("sandbox", temporary=True)]])
        found = machines.list_machines(self.paths, runner)
        self.assertEqual(runner.calls, [([self.hc, "machine", "ls", "--json"], machines.LIST_TIMEOUT)])
        self.assertEqual(found["machines"], [
            {"name": "devbox", "state": "connected", "target": "me@devbox"},
            {"name": "build-1", "state": "asleep", "target": "me@build-1"}])
        self.assertEqual(found["ssh_hosts"], ["lab-2"])
        self.assertNotIn("problem", found)

    def test_an_ssh_alias_is_known_by_name_host_target_or_target_host(self):
        self.ssh_config("Host a1 a2 a3 a4 lab-2\n")
        runner = FakeRunner(listings=[[machine("a1", target="x@one"), machine("devbox", host="a2", target="x@two"),
                                       machine("build-1", target="a3"), machine("other", target="me@a4")]])
        self.assertEqual(machines.list_machines(self.paths, runner)["ssh_hosts"], ["lab-2"])

    def test_list_survives_hc_failing(self):
        self.ssh_config("Host devbox\nHost lab-2\n")
        for reply in [(1, "", "error: no core running\nsecond line"), (0, "not json", ""), (0, '{"x": 1}', ""),
                      (127, "", "No such file or directory")]:
            found = machines.list_machines(self.paths, FakeRunner(listings=[reply]))
            self.assertEqual(found["machines"], [], reply)
            self.assertEqual(found["ssh_hosts"], ["devbox", "lab-2"], reply)
            self.assertTrue(found["problem"], reply)
            self.assertNotIn("\n", found["problem"], reply)
        found = machines.list_machines(self.paths, FakeRunner(listings=[(1, "", "error: no core running\nsecond")]))
        self.assertIn("no core running", found["problem"])
        self.assertNotIn("second", found["problem"])

    def test_inventory_local_uses_sh_and_remote_uses_hc_exec(self):
        runner = FakeRunner(probe=(0, probe_out(), ""))
        machines.inventory(self.paths, None, runner)
        machines.inventory(self.paths, "devbox", runner)
        self.assertEqual(runner.calls, [
            (["/bin/sh", "-c", probe_script()], 45),
            ([self.hc, "exec", "--on", "devbox", "--timeout", "30", "/bin/sh", "-lc", probe_script()], 45)])

    def test_rows_offer_install_for_missing_and_sign_in_for_installed(self):
        runner = FakeRunner(probe=(0, probe_out({"claude": "ready", "codex": "installed"}), ""))
        for where in (None, "devbox"):
            result = machines.inventory(self.paths, where, runner)
            self.assertEqual(result["machine"], where)
            self.assertFalse(result["cached"])
            self.assertTrue(result["has_curl"])
            rows = {row["name"]: row for row in result["harnesses"]}
            self.assertEqual([row["name"] for row in result["harnesses"]], list(KNOWN))
            claude, codex, vibe = rows["claude"], rows["codex"], rows["vibe"]
            self.assertEqual((claude["state"], claude["state_text"], claude["label"]),
                             ("ready", "Ready", "Claude Code"))
            self.assertIsNone(claude["install_argv"])
            self.assertIsNone(claude["install_command"])
            self.assertIsNone(claude["source_url"])
            self.assertIsNone(claude["login_argv"])
            self.assertEqual((codex["state"], codex["state_text"]), ("installed", "Installed, not signed in"))
            self.assertIsNone(codex["install_argv"])
            self.assertEqual(codex["login_argv"], held_argv("codex login --device-auth"))
            self.assertEqual((vibe["state"], vibe["state_text"]), ("missing", "Missing"))
            self.assertIsNone(vibe["login_argv"])
            self.assertEqual(vibe["install_command"], "curl -LsSf https://mistral.ai/vibe/install.sh | bash")
            self.assertEqual(vibe["source_url"], INSTALL["vibe"].source_url)
            if where is None:
                self.assertEqual(vibe["install_argv"],
                                 [str(self.paths.local_bin / "moshi-cockpit"), "harness", "install", "vibe"])
            else:
                self.assertEqual(vibe["install_argv"], held_argv(shown_command(INSTALL["vibe"])))
            self.assertEqual(rows["grok"]["start_argv"], ["/bin/sh", "-lc", f'PATH="$PATH:{PATH_EXTRA}"; exec grok'])

    def test_remote_install_needs_curl(self):
        runner = FakeRunner(probe=(0, probe_out(curl=False), ""))
        remote = machines.inventory(self.paths, "devbox", runner)
        self.assertFalse(remote["has_curl"])
        for row in remote["harnesses"]:
            self.assertIsNone(row["install_argv"], row["name"])
            self.assertEqual(row["source_url"], INSTALL[row["name"]].source_url)
        local = machines.inventory(self.paths, None, runner)  # the local CLI checks for curl itself
        self.assertEqual(local["harnesses"][0]["install_argv"][1:], ["harness", "install", "claude"])

    def test_cache_is_used_within_a_minute_and_refresh_bypasses_it(self):
        runner = FakeRunner(probe=(0, probe_out({"claude": "ready"}), ""))
        first = machines.inventory(self.paths, "devbox", runner, now=lambda: 1000.0)
        self.assertFalse(first["cached"])
        again = machines.inventory(self.paths, "devbox", runner, now=lambda: 1030.0)
        self.assertTrue(again["cached"])
        self.assertEqual(len(runner.probes), 1)
        self.assertEqual(again["harnesses"], first["harnesses"])
        self.assertEqual(again["checked_at"], first["checked_at"])
        self.assertEqual(first["checked_at"], "1970-01-01T00:16:40Z")
        self.assertFalse(machines.inventory(self.paths, None, runner, now=lambda: 1030.0)["cached"])  # own key
        self.assertEqual(len(runner.probes), 2)
        self.assertFalse(machines.inventory(self.paths, "devbox", runner, now=lambda: 1061.0)["cached"])
        self.assertEqual(len(runner.probes), 3)
        self.assertFalse(machines.inventory(self.paths, "devbox", runner, refresh=True, now=lambda: 1062.0)["cached"])
        self.assertEqual(len(runner.probes), 4)

    def test_unreachable_machine_fails_without_touching_the_cache(self):
        machines.inventory(self.paths, "devbox", FakeRunner(probe=(0, probe_out({"claude": "ready"}), "")),
                           now=lambda: 1000.0)
        before = self.cache.read_bytes()
        for reply in [(255, "", "ssh: connect to host devbox port 22: Operation timed out\nmore"),
                      (0, "Welcome to devbox\nlast login yesterday\n", ""), (124, "", "")]:
            with self.assertRaises(ValueError) as caught:
                machines.inventory(self.paths, "devbox", FakeRunner(probe=reply), refresh=True, now=lambda: 2000.0)
            self.assertTrue(str(caught.exception).startswith("devbox did not answer: "), caught.exception)
            self.assertNotIn("Ready", str(caught.exception))
            self.assertEqual(self.cache.read_bytes(), before)
        cut = probe_out({"claude": "ready"}).splitlines()[:2]  # MOTD and the first harness line, then nothing
        with self.assertRaises(ValueError) as caught:
            machines.inventory(self.paths, "devbox", FakeRunner(probe=(0, "\n".join(cut) + "\n", "")), refresh=True)
        self.assertEqual(str(caught.exception), "devbox did not answer: incomplete answer")
        self.assertEqual(self.cache.read_bytes(), before)
        with self.assertRaises(ValueError) as caught:
            machines.inventory(self.paths, None, FakeRunner(probe=(127, "", "")))
        self.assertTrue(str(caught.exception).startswith("This Mac did not answer: "))
        self.assertEqual(self.cache.read_bytes(), before)

    def test_cache_holds_states_only(self):
        runner = FakeRunner(probe=(0, probe_out({"claude": "ready", "codex": "installed"}), ""))
        machines.inventory(self.paths, "devbox", runner, now=lambda: 1000.0)
        machines.inventory(self.paths, None, runner, now=lambda: 1000.0)
        self.assertEqual(stat.S_IMODE(os.stat(self.cache).st_mode), 0o600)
        text = self.cache.read_text()
        data = json.loads(text)
        states = {name: "missing" for name in KNOWN}
        states.update(claude="ready", codex="installed")
        self.assertEqual(data, {"devbox": {"at": 1000.0, "has_curl": True, "states": states},
                                "": {"at": 1000.0, "has_curl": True, "states": states}})
        for leak in ("/bin/sh", "curl ", "PATH", "login", "harness", str(self.paths.home)):
            self.assertNotIn(leak, text)
        self.assertEqual(sorted(p.name for p in self.paths.state.iterdir()),
                         ["inventory.json", "inventory.lock"])  # no temp file left
        self.assertEqual(stat.S_IMODE(os.stat(self.paths.state / "inventory.lock").st_mode), 0o600)

    def test_a_broken_cache_is_probed_again(self):
        self.paths.state.mkdir(parents=True)
        self.cache.write_text('{"devbox": {"at": 1000.0, "has_curl": true, "states": {"claude": "root"}}}')
        runner = FakeRunner(probe=(0, probe_out(), ""))
        self.assertFalse(machines.inventory(self.paths, "devbox", runner, now=lambda: 1001.0)["cached"])
        self.cache.write_text("not json")
        self.assertFalse(machines.inventory(self.paths, "devbox", runner, now=lambda: 1002.0)["cached"])
        self.assertEqual(len(runner.probes), 2)

    def test_add_refuses_an_alias_hc_would_read_as_an_option(self):
        for alias in ["-oProxyCommand=x", "a b", "a;b", "", "$(id)", "a\nb", "devbox\n", "--help", "x" * 300]:
            runner, called = FakeRunner(), []
            with self.assertRaises(ValueError) as caught:
                machines.add_machine(self.paths, alias, called.append, runner, say=lambda _: None)
            self.assertEqual(str(caught.exception), f"'{alias}' cannot be passed on to hc")
            self.assertEqual((runner.calls, called), ([], []), alias)
            with self.assertRaises(ValueError):
                machines.inventory(self.paths, alias or "-", runner)
            self.assertEqual(runner.calls, [], alias)
        for good in ["devbox", "build-1", "me@build-1", "me@10.0.0.7:2222"]:
            self.assertEqual(machines.check_target(good), good)

    def test_add_runs_hc_attached_then_the_inventory(self):
        runner = FakeRunner(probe=(0, probe_out({"claude": "ready", "codex": "installed"}), ""),
                            listings=[[machine("build-1")], [machine("build-1"), machine("devbox")]])
        called, said = [], []

        def call(argv):
            called.append(list(argv))
            return 0

        self.assertEqual(machines.add_machine(self.paths, "devbox", call, runner, say=said.append), 0)
        self.assertEqual(called, [[self.hc, "machine", "add", "--", "devbox"]])
        self.assertEqual(runner.probes, [[self.hc, "exec", "--on", "devbox", "--timeout", "30", "/bin/sh", "-lc",
                                          probe_script()]])
        self.assertEqual(said, [f"{LABELS[n]:<14}{STATE_TEXT[s]}" for n, s in
                                zip(KNOWN, ["ready", "installed", "missing", "missing", "missing", "missing"])])
        self.assertEqual(said[0], "Claude Code   Ready")

        runner, said = FakeRunner(probe=(0, probe_out(), "")), []
        self.assertEqual(machines.add_machine(self.paths, "devbox", lambda argv: 1, runner, say=said.append), 1)
        self.assertEqual((runner.probes, said), ([], []))

    def test_add_finds_the_machine_hc_named_differently(self):
        """`ssh devbox` resolves to me@box; hc names it by that host: the only new entry is the one."""
        runner = FakeRunner(probe=(0, probe_out(), ""),
                            listings=[[machine("build-1")],
                                      [machine("build-1"), machine("box", target="me@box")]])
        said = []
        self.assertEqual(machines.add_machine(self.paths, "devbox", lambda argv: 0, runner, say=said.append), 0)
        self.assertEqual(runner.probes[0][3], "box")
        self.assertEqual(len(said), 6)

    def test_add_reports_a_machine_that_does_not_answer_yet(self):
        runner = FakeRunner(probe=(255, "", "ssh: no route"), listings=[[], [machine("devbox")]])
        said = []
        self.assertEqual(machines.add_machine(self.paths, "devbox", lambda argv: 0, runner, say=said.append), 0)
        self.assertEqual(len(said), 1)
        self.assertTrue(said[0].startswith("devbox was added; devbox did not answer: "), said)
        self.assertFalse(self.cache.exists())

    def test_add_reports_anything_failing_after_hc_added_it(self):
        runner, said = FakeRunner(listings=[[], (1, "", "error: core gone")]), []
        self.assertEqual(machines.add_machine(self.paths, "devbox", lambda argv: 0, runner, say=said.append), 0)
        self.assertEqual(said, ["devbox was added; Herdr's machine list is not available: error: core gone"])
        runner, said = FakeRunner(listings=[[], [machine("x1"), machine("x2")]]), []
        self.assertEqual(machines.add_machine(self.paths, "devbox", lambda argv: 0, runner, say=said.append), 0)
        self.assertEqual(len(said), 1)
        self.assertTrue(said[0].startswith("devbox was added; "), said)
        self.assertEqual(runner.probes, [])

    def test_add_matches_the_alias_only_among_new_machines(self):
        """build-1 was there before and also answers to `devbox`: the new machine is box, not build-1."""
        old = machine("build-1", target="me@devbox")
        runner = FakeRunner(probe=(0, probe_out(), ""), listings=[[old], [old, machine("box", target="me@box")]])
        self.assertEqual(machines.add_machine(self.paths, "devbox", lambda argv: 0, runner, say=lambda _: None), 0)
        self.assertEqual(runner.probes[0][3], "box")
        self.assertEqual(machines._added_name("devbox", {"build-1"}, [old]), None)
        self.assertEqual(machines._added_name("devbox", {"build-1"}, [old, machine("devbox"), machine("y")]),
                         "devbox")
        self.assertEqual(machines._added_name("devbox", None, [machine("y", target="me@devbox"), machine("z")]), "y")

    def test_parallel_inventories_keep_every_machine(self):
        names = [f"build-{i}" for i in range(12)]
        start, errors = threading.Barrier(len(names)), []

        def one(name):
            try:
                start.wait()
                for moment in (1000.0, 2000.0, 3000.0):
                    machines.inventory(self.paths, name, FakeRunner(probe=(0, probe_out(), "")), refresh=True,
                                       now=lambda: moment)
            except Exception as exc:  # noqa: BLE001 - collected for the assertion below
                errors.append(exc)

        threads = [threading.Thread(target=one, args=(name,)) for name in names]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.assertEqual(errors, [])
        data = json.loads(self.cache.read_text())
        self.assertEqual(sorted(data), sorted(names))
        self.assertTrue(all(entry["at"] == 3000.0 for entry in data.values()))
        self.assertEqual(sorted(p.name for p in self.paths.state.iterdir()), ["inventory.json", "inventory.lock"])

    def test_remove_runs_hc_rm_and_drops_only_that_cache_entry(self):
        from cockpit import hosts
        from cockpit.backup import BackupStore
        probe = FakeRunner(probe=(0, probe_out({"claude": "ready"}), ""))
        for name in ("devbox", "build-1", None):
            machines.inventory(self.paths, name, probe, now=lambda: 1000.0)
        hosts.add_host(self.paths, BackupStore(self.paths.state), "devbox")
        saved = (self.paths.mesh_config.parent / "hosts.json").read_bytes()
        runner = FakeRunner(probe=(0, "", ""))
        self.assertEqual(machines.remove_machine(self.paths, "devbox", runner), {"ok": True, "machine": "devbox"})
        self.assertEqual(runner.calls, [([str(self.paths.herdr_core_bin), "machine", "rm", "--", "devbox"],
                                         machines.REMOVE_TIMEOUT)])
        self.assertEqual(machines.REMOVE_TIMEOUT, 30)
        self.assertEqual(sorted(json.loads(self.cache.read_text())), ["", "build-1"])
        self.assertEqual((self.paths.mesh_config.parent / "hosts.json").read_bytes(), saved)  # the chip stays to add it again
        self.assertEqual(machines.remove_machine(self.paths, "lab-2", runner)["ok"], True)  # no cache entry: fine

    def test_remove_refuses_an_option_and_reports_hc_in_one_line(self):
        for name in ["-oProxyCommand=x", "--help", "a b", "a;b", ""]:
            runner = FakeRunner()
            with self.assertRaises(ValueError):
                machines.remove_machine(self.paths, name, runner)
            self.assertEqual(runner.calls, [])
        machines.inventory(self.paths, "devbox", FakeRunner(probe=(0, probe_out(), "")), now=lambda: 1000.0)
        before = self.cache.read_bytes()
        error = json.dumps({"error": {"kind": "not_found", "message": "no machine called devbox"}})
        for reply, said in [((1, error, ""), "devbox was not removed: no machine called devbox"),
                            ((2, "", "hc: boom\nmore"), "devbox was not removed: hc: boom"),
                            ((124, "", ""), "devbox was not removed: exit code 124")]:
            with self.assertRaises(ValueError) as caught:
                machines.remove_machine(self.paths, "devbox", FakeRunner(probe=reply))
            self.assertEqual(str(caught.exception), said)
            self.assertEqual(self.cache.read_bytes(), before)

    def test_a_leftover_temp_file_does_not_break_a_save(self):
        self.paths.state.mkdir(parents=True)
        foreign = self.paths.state / ".inventory.leftover.tmp"
        foreign.write_text("half written")
        os.chmod(foreign, 0o400)
        (self.paths.state / "inventory.json.tmp-moshi-cockpit").mkdir()  # in the way of a fixed temp name
        machines.inventory(self.paths, "devbox", FakeRunner(probe=(0, probe_out(), "")))
        self.assertIn("devbox", json.loads(self.cache.read_text()))
        self.assertEqual(foreign.read_text(), "half written")


class MachineCliTest(unittest.TestCase):
    def setUp(self):
        self.paths = temp_paths(self)

    def run_cli(self, argv, runner):
        out, err = io.StringIO(), io.StringIO()
        with mock.patch("cockpit.cli.Paths.from_env", return_value=self.paths), \
                mock.patch("cockpit.machines.run_capture", runner), \
                mock.patch("cockpit.cli._on_terminal", return_value=False), redirect_stdout(out), redirect_stderr(err):
            code = cli.main(argv)
        return code, out.getvalue(), err.getvalue()

    def test_cli_machine_inventory_json_shape(self):
        runner = FakeRunner(probe=(0, probe_out({"claude": "ready"}), ""))
        code, out, _ = self.run_cli(["machine", "inventory", "--on", "devbox", "--json"], runner)
        self.assertEqual(code, 0)
        data = json.loads(out)
        self.assertEqual(set(data), {"machine", "checked_at", "cached", "has_curl", "harnesses"})
        self.assertEqual(data["machine"], "devbox")
        self.assertEqual(len(data["harnesses"]), 6)
        for row in data["harnesses"]:
            self.assertEqual(set(row), {"name", "label", "state", "state_text", "install_argv", "install_command",
                                        "source_url", "login_argv", "start_argv"})
        code, out, _ = self.run_cli(["machine", "inventory", "--refresh"], runner)
        self.assertEqual(code, 0)
        self.assertIn("Claude Code   Ready", out)
        self.assertEqual(runner.probes[-1][:2], ["/bin/sh", "-c"])

    def test_cli_refuses_an_option_as_machine_name(self):
        runner = FakeRunner(probe=(0, probe_out(), ""))
        code, _, err = self.run_cli(["machine", "inventory", "--on=-oProxyCommand=x"], runner)
        self.assertEqual(code, 1)
        self.assertIn("cannot be passed on to hc", err)
        self.assertEqual(runner.calls, [])

    def test_cli_unreachable_machine_is_one_error_line(self):
        code, out, err = self.run_cli(["machine", "inventory", "--on", "devbox"], FakeRunner(probe=(255, "", "")))
        self.assertEqual((code, out), (1, ""))
        self.assertEqual(err.count("\n"), 1)
        self.assertIn("devbox did not answer", err)

    def test_cli_machine_list(self):
        (self.paths.home / ".ssh").mkdir()
        (self.paths.home / ".ssh" / "config").write_text("Host devbox\nHost lab-2\n")
        runner = FakeRunner(listings=[[machine("devbox")]])
        code, out, _ = self.run_cli(["machine", "list", "--json"], runner)
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(out)["ssh_hosts"], ["lab-2"])
        code, out, _ = self.run_cli(["machine", "list"], runner)
        self.assertEqual(code, 0)
        self.assertIn("devbox", out)
        self.assertIn("lab-2", out)

    def test_cli_machine_add_wires_the_attached_call(self):
        runner = FakeRunner(probe=(0, probe_out(), ""), listings=[[], [machine("devbox")]])
        with mock.patch("cockpit.harness_install.run_argv", return_value=0) as call:
            code, out, _ = self.run_cli(["machine", "add", "devbox"], runner)
        self.assertEqual(code, 0)
        call.assert_called_once_with([str(self.paths.herdr_core_bin), "machine", "add", "--", "devbox"])
        self.assertEqual(len(out.strip().splitlines()), 6)
        with mock.patch("cockpit.harness_install.run_argv", return_value=255):
            code, out, _ = self.run_cli(["machine", "add", "devbox"], FakeRunner())
        self.assertEqual((code, out), (1, ""))


    def test_cli_machine_remove(self):
        runner = FakeRunner(probe=(0, "", ""))
        code, out, _ = self.run_cli(["machine", "remove", "devbox", "--json"], runner)
        self.assertEqual((code, json.loads(out)), (0, {"ok": True, "machine": "devbox"}))
        code, out, _ = self.run_cli(["machine", "remove", "devbox"], runner)
        self.assertEqual((code, out), (0, "devbox removed from Herdr\n"))
        code, out, err = self.run_cli(["machine", "remove", "devbox", "--json"], FakeRunner(probe=(1, "", "gone\n")))
        self.assertEqual((code, out, err), (1, "", "Error: devbox was not removed: gone\n"))
        runner = FakeRunner()
        code, _, err = self.run_cli(["machine", "remove", "--", "-x"], runner)
        self.assertEqual((code, runner.calls), (1, []))
        self.assertIn("cannot be passed on to hc", err)

if __name__ == "__main__":
    unittest.main()
