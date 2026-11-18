"""`machine cockpit | agent`: moshi-cockpit on a remote Linux machine, read and driven through `hc exec`, with fake
runners (nothing real runs: no hc, no ssh, no /bin/sh) and a temp HOME."""

import argparse
import io
import json
import shutil
import stat
import subprocess
import unittest
from contextlib import redirect_stderr, redirect_stdout
from unittest import mock

import cockpit
from cockpit import cli, remote
from cockpit.agents import AgentContext
from cockpit.backup import BackupStore
from cockpit.secrets import KeychainError
from cockpit.secrets.macos import MacKeychain
from tests.helpers import MOSHI, FakeSecurity, configure, temp_paths

MARK = remote.MARK


def listing(agents=(), connected=True, version=None, ocellio=False, harnesses=None):
    """What the remote `agent list --json` prints (Task 3's shape)."""
    rows = harnesses if harnesses is not None else [
        {"name": "codex", "installed": True, "available": True, "reason": "", "native_mesh": True}]
    return json.dumps({"agents": list(agents), "harnesses": rows, "servers": {"moshi": True, "ocellio": ocellio},
                       "version": version or cockpit.__version__, "connected": connected})


def probe_out(os_name="Linux", answer=None, noise=True, end=True):
    """What the probe prints: marker lines, MOTD noise around them. `answer` None means not installed."""
    lines = ["Welcome to devbox", "Last login: yesterday"] if noise else []
    lines.append(f"{MARK}\tos\t{os_name}")
    lines.append(f"{MARK}\tlist\t{'missing' if answer is None else answer}")
    if end:
        lines.append(f"{MARK}\tend")
    if noise:
        lines.append("Have a nice day")
    return "\n".join(lines) + "\n"


def agent(name, harness="codex", role=""):
    return {"name": name, "harness": harness, "role": role, "skill": "", "installed": True}


class FakeRunner:
    """Records (argv, timeout); answers from `replies`, the first whose key is part of the argv, else `default`."""

    def __init__(self, default=(0, "", ""), replies=None):
        self.calls, self.default, self.replies = [], default, dict(replies or {})

    def __call__(self, argv, timeout):
        self.calls.append((list(argv), timeout))
        for key, reply in self.replies.items():
            if key in argv:
                return reply
        return self.default


class RemoteTest(unittest.TestCase):
    def setUp(self):
        self.paths = temp_paths(self)
        configure(self.paths, servers={"moshi": MOSHI})
        self.hc = str(self.paths.herdr_core_bin)
        self.cache = self.paths.state / remote.CACHE_FILE

    def state(self, answer, machine="devbox", os_name="Linux", **options):
        runner = FakeRunner((0, probe_out(os_name, answer), ""))
        return remote.cockpit_state(self.paths, machine, runner, **options), runner

    def test_probe_argv_is_static_and_the_name_is_checked(self):
        argv = remote.probe_argv(self.paths, "devbox")
        self.assertEqual(argv[:6], [self.hc, "exec", "--on", "devbox", "--timeout", "30"])
        self.assertEqual(argv[6:], ["/bin/sh", "-lc", remote.PROBE_SCRIPT])
        self.assertNotIn("devbox", remote.PROBE_SCRIPT)
        runner = FakeRunner()
        for name in ("-oProxyCommand=x", "a b", "x;y", ""):
            with self.assertRaises(ValueError):
                remote.cockpit_state(self.paths, name, runner)
        self.assertEqual(runner.calls, [])

    def test_missing_cockpit_offers_install_with_this_macs_version_and_servers(self):
        found, runner = self.state(None)
        self.assertEqual(runner.calls, [(remote.probe_argv(self.paths, "devbox"), remote.PROBE_TIMEOUT)])
        self.assertEqual((found["os"], found["state"], found["update"]), ("Linux", "missing", False))
        self.assertEqual(found["state_text"], "moshi-cockpit not installed")
        self.assertIsNone(found["connect_argv"])
        self.assertEqual(found["install_argv"][:4], ["/bin/sh", "-lc", remote.INSTALL_SCRIPT, "sh"])
        self.assertEqual(found["install_argv"][4:], [remote.INSTALL_URL, "https://moshi.example.test/mcp", ""])
        self.assertIn(f"/v{cockpit.__version__}/install.sh", remote.INSTALL_URL)
        self.assertIn("mktemp", found["install_argv"][2])
        self.assertEqual(found["agents"], [])

    def run_install(self, moshi, ocellio):
        """INSTALL_SCRIPT under /bin/sh with a fake curl that 'downloads' a fake install.sh recording its
        arguments; temp folder only, no network. Returns (install.sh's arguments, curl's arguments, output)."""
        root = self.paths.home / "install-run"
        bin_dir, tmp = root / "bin", root / "tmp"
        bin_dir.mkdir(parents=True)
        tmp.mkdir()
        (root / "install.sh").write_text('#!/bin/sh\n: > "$ARGS_FILE"\nfor a in "$@"; do printf "%s\\n" "$a" >> "$ARGS_FILE"; done\n')
        curl = bin_dir / "curl"
        curl.write_text('#!/bin/sh\nprintf "%s\\n" "$@" > "$CURL_FILE"\n'
                        'while [ $# -gt 0 ]; do [ "$1" = -o ] && out=$2; shift; done\ncp "$FAKE_INSTALL" "$out"\n')
        curl.chmod(0o755)
        env = {"PATH": f"{bin_dir}:/usr/bin:/bin", "TMPDIR": str(tmp), "HOME": str(self.paths.home),
               "ARGS_FILE": str(root / "args"), "CURL_FILE": str(root / "curl-args"),
               "FAKE_INSTALL": str(root / "install.sh")}
        done = subprocess.run(["/bin/sh", "-c", remote.INSTALL_SCRIPT, "sh", remote.INSTALL_URL, moshi, ocellio],
                              env=env, stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=20)
        self.assertEqual(list(tmp.iterdir()), [])  # the downloaded file is gone
        args = (root / "args").read_text().splitlines()
        curl_args = (root / "curl-args").read_text().splitlines()
        shutil.rmtree(str(root))
        return args, curl_args, done.stdout

    def test_install_passes_only_the_servers_this_mac_has(self):
        args, curl_args, out = self.run_install(MOSHI, "")
        self.assertEqual(args, ["--moshi", MOSHI])  # an empty ocellio keeps what the machine has
        self.assertEqual(curl_args[:3], ["--proto", "=https", "--tlsv1.2"])
        self.assertEqual(curl_args[-1], remote.INSTALL_URL)
        self.assertIn("Finished (exit 0). Press Enter to close.", out)
        self.assertEqual(self.run_install("", "")[0], [])
        self.assertEqual(self.run_install("", "https://o.example.test/mcp/")[0], ["--ocellio", "https://o.example.test/mcp/"])

    def test_unconnected_offers_connect_with_the_machine_name(self):
        found, _ = self.state(listing(connected=False))
        self.assertEqual(found["state"], "unconnected")
        self.assertEqual(found["state_text"], "moshi-cockpit not connected to moshi")
        self.assertEqual(found["connect_argv"], ["/bin/sh", "-lc",
                                                 'exec "$HOME/.local/bin/moshi-cockpit" connect --label "$1"',
                                                 "sh", "devbox"])
        self.assertIsNone(found["install_argv"])

    def test_connected_lists_validated_agents(self):
        rows = [agent("ok-1", role="a\x1b]0;x\x07b"), agent("bad name"), agent("x", harness="nope")]
        found, _ = self.state(listing(rows, ocellio=True))
        self.assertEqual(found["state"], "connected")
        self.assertEqual(found["agents"], [{"name": "ok-1", "harness": "codex", "role": "a ]0;x b",
                                            "run_argv": remote.run_argv("ok-1")}])
        self.assertEqual(remote.run_argv("ok-1"),
                         ["/bin/sh", "-lc", 'exec "$HOME/.local/bin/moshi-cockpit" run "$1"', "sh", "ok-1"])
        self.assertEqual(found["state_text"], "moshi-cockpit connected (1 agent)")
        self.assertEqual(found["version"], cockpit.__version__)
        self.assertIs(found["ocellio"], True)
        self.assertEqual(found["harnesses"], [{"name": "codex", "installed": True, "available": True, "reason": ""}])
        self.assertIsNotNone(found["connect_argv"])
        self.assertIsNone(found["install_argv"])

    def test_harness_rows_are_validated(self):
        rows = [{"name": "codex", "installed": "yes", "available": True, "reason": "x\x1b[2Jy"},
                {"name": "nope", "installed": True, "available": True}, "junk"]
        found, _ = self.state(listing([agent("a"), agent("b")], harnesses=rows))
        self.assertEqual(found["harnesses"], [{"name": "codex", "installed": False, "available": True,
                                               "reason": "x [2Jy"}])
        self.assertEqual(found["state_text"], "moshi-cockpit connected (2 agents)")

    def test_other_os_has_no_actions(self):
        found, _ = self.state(listing(), os_name="Darwin")
        self.assertEqual((found["os"], found["state"], found["state_text"]), ("Darwin", "unsupported", ""))
        self.assertEqual((found["install_argv"], found["connect_argv"], found["update"]), (None, None, False))
        self.assertEqual(found["agents"], [])

    def test_version_mismatch_offers_update(self):
        found, _ = self.state(listing(connected=False, version="0.0.1"))
        self.assertEqual((found["version"], found["update"]), ("0.0.1", True))
        self.assertEqual(found["install_argv"][4], remote.INSTALL_URL)
        self.assertEqual(found["state_text"], f"moshi-cockpit not connected to moshi · update to {cockpit.__version__}")
        older = json.loads(listing(connected=False))
        del older["version"]  # a moshi-cockpit from before the version field
        found, _ = self.state(json.dumps(older), refresh=True)
        self.assertEqual((found["version"], found["update"]), (None, True))
        found, _ = self.state(listing(version="1.2.3-evil"), refresh=True)
        self.assertEqual((found["version"], found["update"]), (None, True))

    def test_noise_around_the_probe_is_ignored(self):
        out = ("Welcome\nmoshi-cockpit-remote is great\n" + probe_out(answer="{bad") +
               f"{MARK}\tlist\t{listing([agent('ok-1')])}\n")
        found = remote.cockpit_state(self.paths, "devbox", FakeRunner((0, out, "")))
        self.assertEqual(found["state"], "error")
        self.assertEqual(found["agents"], [])
        self.assertTrue(found["state_text"])
        parsed = remote.parse_probe(probe_out(answer=listing([agent("ok-1")])))
        self.assertEqual((parsed["os"], parsed["state"]), ("Linux", "connected"))
        for answer in ("", "[]", '{"agents": "x"}'):
            self.assertEqual(remote.parse_probe(probe_out(answer=answer))["state"], "error")
        with self.assertRaises(ValueError) as caught:
            remote.cockpit_state(self.paths, "build-1", FakeRunner((0, probe_out(answer="missing", end=False), "")))
        self.assertEqual(str(caught.exception), "build-1 did not answer: incomplete answer")

    def test_failed_probe_keeps_the_cache(self):
        self.state(listing([agent("ok-1")]))
        before = self.cache.read_text()
        self.assertEqual(stat.S_IMODE(self.cache.stat().st_mode), 0o600)
        runner = FakeRunner((255, "", "ssh: connect to host devbox port 22: Connection refused\n"))
        with self.assertRaises(ValueError) as caught:
            remote.cockpit_state(self.paths, "devbox", runner, refresh=True)
        self.assertEqual(str(caught.exception), "devbox did not answer: ssh: connect to host devbox port 22: "
                                                "Connection refused")
        self.assertEqual(self.cache.read_text(), before)
        self.assertEqual([a["name"] for a in remote.cached_agents(self.paths)], ["ok-1"])

    def test_cache_within_a_minute_and_refresh(self):
        clock = [1000.0]
        runner = FakeRunner((0, probe_out(answer=listing([agent("ok-1")])), ""))
        first = remote.cockpit_state(self.paths, "devbox", runner, now=lambda: clock[0])
        clock[0] += 59
        second = remote.cockpit_state(self.paths, "devbox", runner, now=lambda: clock[0])
        self.assertEqual(len(runner.calls), 1)
        self.assertEqual((first["cached"], second["cached"]), (False, True))
        self.assertEqual(second["agents"], first["agents"])
        remote.cockpit_state(self.paths, "devbox", runner, refresh=True, now=lambda: clock[0])
        self.assertEqual(len(runner.calls), 2)
        clock[0] += 61
        remote.cockpit_state(self.paths, "devbox", runner, now=lambda: clock[0])
        self.assertEqual(len(runner.calls), 3)
        remote.cockpit_state(self.paths, "build-1", runner, now=lambda: clock[0])  # one entry per machine
        self.assertEqual(sorted(json.loads(self.cache.read_text())), ["build-1", "devbox"])

    def add(self, runner, **options):
        values = dict(name="ok-1", harness="codex", role='reviews; echo "$HOME" `id`', skill="")
        values.update(options)
        return remote.agent_call(self.paths, "devbox", "add", runner, **values)

    def test_agent_call_passes_argv_intact(self):
        runner = FakeRunner((0, "• Agent ok-1 (codex) created\n", ""))
        self.add(runner)
        (argv, timeout), = runner.calls
        self.assertEqual(timeout, remote.AGENT_TIMEOUT)
        self.assertEqual(argv[:9], [self.hc, "exec", "--on", "devbox", "--timeout", "90", "/bin/sh", "-lc",
                                    remote.CLI_SCRIPT])
        self.assertEqual(argv[8], 'c="$HOME/.local/bin/moshi-cockpit"; [ -x "$c" ] || exit 127; exec "$c" "$@"')
        self.assertEqual(argv[9:], ["sh", "agent", "add", "ok-1", "--harness", "codex",
                                    '--role=reviews; echo "$HOME" `id`', "--skill="])
        for role in ("-x", "--", "--harness=claude"):  # one argument each, never read as an option there
            self.add(runner, role=role, skill="skill-a")
            self.assertEqual(runner.calls[-1][0][-2:], [f"--role={role}", "--skill=skill-a"])
        del runner.calls[1:]
        remote.agent_call(self.paths, "devbox", "load", runner, name="ok-2")
        remote.agent_call(self.paths, "devbox", "catalog", runner)
        self.assertEqual(runner.calls[1][0][9:], ["sh", "agent", "load", "ok-2"])
        self.assertEqual(runner.calls[2][0][9:], ["sh", "agent", "catalog", "--json"])

    def test_agent_call_validates_before_any_call(self):
        runner = FakeRunner()
        for options in (dict(name="bad name"), dict(name="-x"), dict(harness="grok"), dict(harness="nope"),
                        dict(role="a\nb"), dict(skill="s\x1b"), dict(role="r" * 301), dict(skill="-x"),
                        dict(skill="a b"), dict(skill="a;b")):
            with self.assertRaises(ValueError, msg=options):
                self.add(runner, **options)
        with self.assertRaises(ValueError):
            remote.agent_call(self.paths, "-oProxyCommand=x", "catalog", runner)
        with self.assertRaises(ValueError):
            remote.agent_call(self.paths, "devbox", "remove", runner, name="ok-1")
        with self.assertRaises(ValueError):
            remote.agent_call(self.paths, "devbox", "load", runner, name=None)
        self.assertEqual(runner.calls, [])

    def test_agent_call_maps_exit_codes(self):
        def fresh_cache():
            self.state(listing([agent("ok-1")]), refresh=True)
            self.assertIn("devbox", json.loads(self.cache.read_text()))

        fresh_cache()
        out = "Welcome to devbox\n• Agent ok-1 (codex) created in moshi and here\n• Start it: moshi-cockpit run ok-1\nbye\n"
        code, answer = self.add(FakeRunner((0, out, "")))
        self.assertEqual((code, answer["ok"], answer["code"]), (0, True, 0))
        self.assertEqual(answer["notes"], ["Agent ok-1 (codex) created in moshi and here",
                                           "Start it: moshi-cockpit run ok-1"])
        self.assertEqual(answer["run_argv"], remote.run_argv("ok-1"))
        self.assertEqual(answer["message"], "")
        self.assertNotIn("devbox", json.loads(self.cache.read_text()))

        fresh_cache()
        code, answer = self.add(FakeRunner((3, "", "Approval needed: connect this Herdr again\n")))
        self.assertEqual((code, answer["ok"], answer["run_argv"]), (3, False, None))
        self.assertEqual(answer["message"], "Approval needed: connect this Herdr again")
        self.assertNotIn("devbox", json.loads(self.cache.read_text()))

        fresh_cache()
        locale = "bash: warning: setlocale: LC_ALL: cannot change locale (en_US.UTF-8)\n"
        code, answer = self.add(FakeRunner((1, "", locale + "Error: Agent 'ok-1' exists already\nmore\n")))
        self.assertEqual((code, answer["ok"], answer["message"]), (1, False, "Agent 'ok-1' exists already"))
        self.assertNotIn("devbox", json.loads(self.cache.read_text()))
        code, answer = self.add(FakeRunner((3, "", locale + "This Herdr needs a new approval from moshi: run x.\n")))
        self.assertEqual(answer["message"], "This Herdr needs a new approval from moshi: run x.")
        code, answer = self.add(FakeRunner((1, "", locale + "something else\n")))
        self.assertEqual(answer["message"], locale.strip())

        fresh_cache()  # no hc on this Mac (the temp HOME has none): never blamed on the machine
        code, answer = self.add(FakeRunner((127, "", "No such file or directory\n")))
        self.assertEqual((code, answer["ok"], answer["message"]), (1, False, "Herdr's hc is not available on this Mac"))
        self.assertIn("devbox", json.loads(self.cache.read_text()))
        with self.assertRaises(ValueError) as caught:
            remote.cockpit_state(self.paths, "devbox", FakeRunner((127, "", "")), refresh=True)
        self.assertEqual(str(caught.exception), "Herdr's hc is not available on this Mac")

        self.paths.herdr_core_bin.parent.mkdir(parents=True, exist_ok=True)
        self.paths.herdr_core_bin.write_text("")  # hc is there: 127 came from the machine
        code, answer = self.add(FakeRunner((127, "", "sh: 1: exec: not found\n")))
        self.assertEqual((code, answer["notes"]), (1, []))
        self.assertEqual(answer["message"], "moshi-cockpit is not installed on devbox: install it under Machines")
        self.assertIn("devbox", json.loads(self.cache.read_text()))  # it never reached moshi-cockpit there

        for reply in ((255, "• Agent ok-1 created\n", "ssh: Connection reset\n"), (124, "", ""), (2, "", "x\n")):
            fresh_cache()
            code, answer = self.add(FakeRunner(reply))
            self.assertEqual((code, answer["ok"], answer["notes"], answer["run_argv"]), (1, False, [], None))
            self.assertTrue(answer["message"].startswith("devbox did not answer: "), answer["message"])
            self.assertIn("devbox", json.loads(self.cache.read_text()))  # it never reached the remote
        self.assertEqual(answer["message"], "devbox did not answer: x")

    def test_catalog_rows_are_validated(self):
        rows = [{"name": "scout", "harness": "codex", "role": "Research", "skill": "", "active_installation": "Lab\x1b[31m",
                 "harness_installed": True, "loadable": True, "reason": ""},
                {"name": "bad name", "harness": "codex", "role": "", "active_installation": None,
                 "harness_installed": True, "loadable": True, "reason": ""},
                {"name": "other", "harness": "nope", "active_installation": None, "loadable": False,
                 "harness_installed": False, "reason": "harness nope is unknown here"}]
        out = "MOTD {not json\n" + json.dumps({"agents": rows}) + "\n"
        code, answer = remote.agent_call(self.paths, "devbox", "catalog", FakeRunner((0, out, "")))
        self.assertEqual((code, answer["ok"], answer["run_argv"]), (0, True, None))
        self.assertEqual(answer["agents"], [
            {"name": "scout", "harness": "codex", "role": "Research", "active_installation": "Lab [31m",
             "loadable": True, "harness_installed": True, "reason": ""},
            {"name": "other", "harness": "nope", "role": "", "active_installation": None, "loadable": False,
             "harness_installed": False, "reason": "harness nope is unknown here"}])
        code, answer = remote.agent_call(self.paths, "devbox", "catalog", FakeRunner((0, "MOTD\n", "")))
        self.assertEqual((code, answer["agents"]), (1, []))
        self.assertTrue(answer["message"].startswith("devbox did not answer"))
        code, answer = remote.agent_call(self.paths, "devbox", "catalog", FakeRunner((3, "", "Approval needed\n")))
        self.assertEqual((code, answer["agents"], answer["message"]), (3, [], "Approval needed"))

    def run_cli(self, argv, runner):
        out, err = io.StringIO(), io.StringIO()
        keychain = MacKeychain(FakeSecurity())
        with mock.patch("cockpit.cli.Paths.from_env", return_value=self.paths), \
                mock.patch("cockpit.machines.run_capture", runner), \
                mock.patch("cockpit.cli.default_store", return_value=keychain), \
                mock.patch("cockpit.cli._on_terminal", return_value=False), redirect_stdout(out), redirect_stderr(err):
            code = cli.main(argv)
        return code, out.getvalue(), err.getvalue()

    def test_status_json_lists_cached_remote_agents(self):
        configure(self.paths, servers={})
        self.state(listing([agent("ok-1")]), machine="build-1")
        self.state(listing([agent("ok-2")], connected=False), machine="devbox")  # not connected: not startable
        code, out, err = self.run_cli(["status", "--json"], FakeRunner())
        self.assertEqual(code, 0, err)
        self.assertEqual(json.loads(out)["remote"], [{"name": "ok-1", "harness": "codex", "role": "",
                                                      "machine": "build-1", "run_argv": remote.run_argv("ok-1")}])

    def test_cached_agents_ignore_age_and_sort_by_machine(self):
        self.state(listing([agent("zed"), agent("abc")]), machine="devbox", now=lambda: 1.0)
        self.state(listing([agent("ok-1")]), machine="build-1")
        self.assertEqual([(a["machine"], a["name"]) for a in remote.cached_agents(self.paths)],
                         [("build-1", "ok-1"), ("devbox", "abc"), ("devbox", "zed")])
        self.cache.write_text("{broken")
        self.assertEqual(remote.cached_agents(self.paths), [])

    def test_machine_remove_forgets_the_remote_state(self):
        self.state(listing([agent("ok-1")]), machine="devbox")
        self.state(listing([agent("ok-2")]), machine="build-1")
        code, _, err = self.run_cli(["machine", "remove", "devbox"], FakeRunner((0, "", "")))
        self.assertEqual(code, 0, err)
        self.assertEqual(sorted(json.loads(self.cache.read_text())), ["build-1"])
        remote.forget(self.paths, "build-1")
        self.assertEqual(remote.cached_agents(self.paths), [])

    def test_cli_machine_cockpit_and_agent(self):
        runner = FakeRunner((0, probe_out(answer=listing([agent("ok-1")])), ""))
        code, out, _ = self.run_cli(["machine", "cockpit", "--on", "devbox", "--json"], runner)
        self.assertEqual((code, json.loads(out)["state"]), (0, "connected"))
        code, out, _ = self.run_cli(["machine", "cockpit", "--on", "devbox"], runner)
        self.assertEqual((code, out), (0, "devbox: moshi-cockpit connected (1 agent)\n"))
        self.assertEqual(len(runner.calls), 1)  # the second one came from the cache
        code, _, err = self.run_cli(["machine", "cockpit", "--on=-x"], FakeRunner())
        self.assertEqual(code, 1)
        self.assertIn("cannot be passed on to hc", err)

        runner = FakeRunner((3, "", "Approval needed\n"))
        code, out, _ = self.run_cli(["machine", "agent", "--on", "devbox", "add", "ok-1", "--harness", "codex",
                                     "--json"], runner)
        self.assertEqual((code, json.loads(out)["code"]), (3, 3))
        self.assertEqual(runner.calls[0][0][9:], ["sh", "agent", "add", "ok-1", "--harness", "codex", "--role=",
                                                  "--skill="])
        code, out, err = self.run_cli(["machine", "agent", "--on", "devbox", "load", "ok-1"],
                                      FakeRunner((0, "• Agent ok-1 (codex) loaded from moshi\n", "")))
        self.assertEqual((code, out, err), (0, "• Agent ok-1 (codex) loaded from moshi\n", ""))
        code, out, err = self.run_cli(["machine", "agent", "--on", "devbox", "load", "ok-1"], FakeRunner((255, "", "")))
        self.assertEqual((code, out), (1, ""))
        self.assertEqual(err, "devbox did not answer: exit code 255\n")


class AgentListStoreDownTest(unittest.TestCase):
    """Carried from Task 3: on the remote, a store that fails still gives the probe a state, not "did not answer"."""

    def test_agent_list_json_survives_a_keychain_error(self):
        paths = temp_paths(self)
        configure(paths, servers={"moshi": MOSHI}, agents=[{"name": "ok-1", "harness": "codex"}])

        class Down:
            name = "test store"

            def exists(self, service, account):
                raise KeychainError("Secret Service entry is locked or not answering")

            read = write = delete = exists

        ctx = AgentContext(paths=paths, backup=BackupStore(paths.state), keychain=Down())
        out = io.StringIO()
        with redirect_stdout(out):
            self.assertEqual(cli._agent_list(ctx, argparse.Namespace(json=True)), 0)
        data = json.loads(out.getvalue())
        self.assertIs(data["connected"], False)
        self.assertEqual([(a["name"], a["connected"], a["has_moshi_token"]) for a in data["agents"]],
                         [("ok-1", False, False)])
        self.assertEqual(remote.parse_probe(probe_out(answer=out.getvalue().replace("\n", "")))["state"], "unconnected")


if __name__ == "__main__":
    unittest.main()
