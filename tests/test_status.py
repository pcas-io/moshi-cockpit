"""The mesh read with the device key (`status`, `agent where`) and the takeover at `run`. FakeMoshi, fake keychain."""

import json
import socket
import unittest
from unittest import mock

from cockpit import agents_remote, launch, status
from cockpit.moshi_api import MoshiApi
from tests.test_agents_remote import ConnectedHerdr

UNREACHABLE_LINE = "moshi is not reachable; starting without the mesh."


def closed_port_api():
    """A MoshiApi for a local port nobody listens on: every call ends as `unreachable`."""
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    return MoshiApi(f"http://127.0.0.1:{port}/mcp", timeout=2)


class StatusTest(ConnectedHerdr, unittest.TestCase):
    def status_json(self):
        code, out, err = self.cli("status", "--json")
        self.assertEqual(code, 0, err)
        return json.loads(out)

    def test_status_reads_through_the_device_key(self):
        agents_remote.create(self.ctx, "scout", "codex", "Research", "skill-a", api=self.api)
        self.moshi.agent("scout").update(presence="live", working_on="reading", last_seen_at="2026-11-17T09:00:00.000Z")
        other, _ = self.moshi.add_installation("Home Mac")
        self.moshi.add_agent("helper", harness="vibe", installation_id=other["id"])
        self.moshi.agent("helper")["active_installation_id"] = other["id"]
        self.moshi.requests.clear()
        data = self.status_json()
        self.assertEqual(data["installation"], {"id": self.installation["id"], "label": "Work Mac"})
        rows = {a["name"]: a for a in data["agents"]}
        self.assertEqual(rows["scout"], {"name": "scout", "role": "Research", "presence": "live", "working_on": "reading",
                                         "last_seen_at": "2026-11-17T09:00:00.000Z", "kind": "local",
                                         "harness": "codex", "skill": "skill-a", "active_installation": "Work Mac"})
        self.assertEqual(rows["helper"]["active_installation"], "Home Mac")
        self.assertEqual((data["approval_valid"], data["disconnected"], data["unreachable"], data["needs_connect"]),
                         (True, False, False, False))
        self.assertEqual([(r["method"], r["path"], r["auth"]) for r in self.moshi.requests],
                         [("GET", "/api/installation/status", f"Bearer {self.device_key}")])  # never /mcp
        self.assertEqual([a["name"] for a in data["local"]], ["scout"])
        self.assertNotIn("moshi_name", data["local"][0])
        self.assertNotIn("identity", data)
        for key in ("servers", "harnesses", "problems"):
            self.assertIn(key, data)

    def test_status_marks_a_revoked_device_key_as_disconnected(self):
        self.moshi.revoke_installation(self.installation["id"])
        data = self.status_json()
        self.assertEqual((data["disconnected"], data["unreachable"], data["needs_connect"]), (True, False, False))
        self.assertEqual((data["agents"], data["installation"]), ([], None))

    def test_status_marks_an_unreachable_moshi(self):
        self.api = closed_port_api()
        data = self.status_json()
        self.assertEqual((data["unreachable"], data["disconnected"]), (True, False))
        self.assertEqual((data["agents"], data["installation"]), ([], None))

    def test_status_without_a_device_key_asks_nothing(self):
        del self.fake.entries[("moshi-device", "device")]
        data = self.status_json()
        self.assertEqual((data["needs_connect"], data["disconnected"]), (True, False))
        self.assertEqual(self.moshi.requests, [])

    def test_status_reports_an_expired_approval(self):
        del self.fake.entries[("moshi-register", "register")]
        self.assertIs(self.status_json()["approval_valid"], False)

    def test_the_text_status_names_this_herdr_and_where_agents_run(self):
        agents_remote.create(self.ctx, "scout", "codex", api=self.api)
        code, out, _ = self.cli("status")
        self.assertEqual(code, 0)
        self.assertIn("scout", out)
        self.assertIn("'Work Mac'", out)
        self.assertNotIn("refreshes", out)

    def test_the_text_status_says_disconnected_and_unreachable(self):
        self.api = closed_port_api()
        self.assertIn("not reachable", self.cli("status")[1])
        self.moshi.revoke_installation(self.installation["id"])
        self.api = MoshiApi(self.moshi.url)
        self.assertIn("Disconnected from moshi", self.cli("status")[1])

    def test_status_text_from_the_server_is_cleaned(self):
        self.moshi.add_agent("evil", role="x\x1b[2J")
        self.moshi.agent("evil")["working_on"] = "line1\r\nline2\x1b]0;pwned\x07"
        data = self.status_json()
        text = json.dumps(data) + self.cli("status")[1]
        for bad in ("\x1b", "\x07", "\r"):
            self.assertNotIn(bad, text)


class WhereTest(ConnectedHerdr, unittest.TestCase):
    def setUp(self):
        super().setUp()
        agents_remote.create(self.ctx, "scout", "codex", api=self.api)
        self.other, _ = self.moshi.add_installation("Home Mac")

    def where(self):
        code, out, err = self.cli("agent", "where", "scout", "--json")
        self.assertEqual(code, 0, err)
        return json.loads(out)

    def test_where_names_this_installation(self):
        self.assertEqual(self.where(), {"active": "Work Mac", "here": True})

    def test_where_names_another_installation(self):
        self.moshi.agent("scout")["active_installation_id"] = self.other["id"]
        self.assertEqual(self.where(), {"active": "Home Mac", "here": False})

    def test_where_with_nobody_holding_it(self):
        self.moshi.agent("scout")["active_installation_id"] = None
        self.assertEqual(self.where(), {"active": None, "here": False})

    def test_where_without_a_device_key_asks_for_an_approval(self):
        del self.fake.entries[("moshi-device", "device")]
        self.assertEqual(self.cli("agent", "where", "scout", "--json")[0], 3)

    def test_where_reports_an_unreachable_moshi_in_one_line(self):
        self.api = closed_port_api()
        code, out, err = self.cli("agent", "where", "scout", "--json")
        self.assertEqual((code, out), (1, ""))
        self.assertIn("Could not reach moshi", err)


class RunTakeoverTest(ConnectedHerdr, unittest.TestCase):
    def setUp(self):
        super().setUp()
        agents_remote.create(self.ctx, "scout", "codex", api=self.api)
        self.token = self.fake.entries[("moshi-scout", "scout")]
        self.other, _ = self.moshi.add_installation("Home Mac")
        self.moshi.requests.clear()

    def run_cli(self, *argv, terminal=False, answers=()):
        """`run` with the start prepared and exec'd by fakes; `started` records what would replace the process.
        terminal=True: a TTY on both ends, `answers` are typed at each prompt."""
        typed = iter(answers)
        prompts = []

        def ask(prompt=""):
            prompts.append(prompt)
            return next(typed)

        with mock.patch.object(launch, "prepare", side_effect=lambda paths, keychain, name, extra: (name, list(extra))), \
                mock.patch.object(launch, "execute") as started, \
                mock.patch("cockpit.cli._on_terminal", return_value=terminal), mock.patch("builtins.input", ask):
            code, out, err = self.cli("run", *argv)
        self.prompts = prompts
        return code, out, err, started

    def holder(self):
        return self.moshi.agent("scout")["active_installation_id"]

    def test_run_exits_4_when_another_installation_is_active(self):
        self.moshi.agent("scout")["active_installation_id"] = self.other["id"]
        code, out, err, started = self.run_cli("scout")
        self.assertEqual(code, 4, err)
        self.assertEqual(json.loads(out), {"active": "Home Mac"})
        self.assertIn("moshi-cockpit run scout --take-over", err)
        started.assert_not_called()
        self.assertEqual(self.moshi.calls("POST", "/api/agent/activate"), [])
        self.assertEqual(self.holder(), self.other["id"])

    def test_run_with_take_over_activates_and_starts(self):
        self.moshi.agent("scout")["active_installation_id"] = self.other["id"]
        code, out, err, started = self.run_cli("scout", "--take-over", "exec", "hi")
        self.assertEqual((code, out), (0, ""), err)
        self.assertEqual([c["auth"] for c in self.moshi.calls("POST", "/api/agent/activate")], [f"Bearer {self.token}"])
        self.assertEqual(self.moshi.calls("GET", "/api/installation/status")[0]["auth"], f"Bearer {self.device_key}")
        self.assertEqual(self.holder(), self.installation["id"])
        self.assertEqual(started.call_args.args[0], ("scout", ["exec", "hi"]))
        self.assert_no_secret(out, err)

    def test_take_over_also_works_before_the_agent_name(self):
        self.moshi.agent("scout")["active_installation_id"] = self.other["id"]
        code, _, err, started = self.run_cli("--take-over", "scout", "exec")
        self.assertEqual(code, 0, err)
        self.assertEqual(self.holder(), self.installation["id"])
        self.assertEqual(started.call_args.args[0], ("scout", ["exec"]))

    def test_run_activates_silently_when_nobody_holds_the_agent(self):
        self.moshi.agent("scout")["active_installation_id"] = None  # freshly loaded: not_active until activated
        code, out, err, started = self.run_cli("scout")
        self.assertEqual((code, out, err), (0, "", ""))
        self.assertEqual(self.holder(), self.installation["id"])
        started.assert_called_once()

    def test_run_activates_silently_when_the_agent_is_here(self):
        code, out, err, started = self.run_cli("scout")
        self.assertEqual((code, out, err), (0, "", ""))
        self.assertEqual(len(self.moshi.calls("POST", "/api/agent/activate")), 1)
        started.assert_called_once()

    def test_a_takeover_between_the_check_and_the_start_is_named(self):
        self.moshi.agent("scout")["active_installation_id"] = self.other["id"]  # started there after the check
        with mock.patch.object(status, "where", return_value={"active": None, "here": False, "own": "Work Mac"}):
            code, out, err, started = self.run_cli("scout")
        self.assertEqual((code, out), (0, ""), err)
        self.assertIn("scout was held by Home Mac until just now", err)
        self.assertEqual(self.holder(), self.installation["id"])
        started.assert_called_once()

    def test_a_confirmed_takeover_is_not_named_again(self):
        self.moshi.agent("scout")["active_installation_id"] = self.other["id"]
        code, _, err, _ = self.run_cli("scout", "--take-over")
        self.assertEqual(code, 0, err)
        self.assertNotIn("until just now", err)

    def test_run_starts_the_agent_when_moshi_is_unreachable(self):
        self.api = closed_port_api()
        code, out, err, started = self.run_cli("scout", "exec")
        self.assertEqual((code, out, err.strip()), (0, "", UNREACHABLE_LINE))
        self.assertEqual(started.call_args.args[0], ("scout", ["exec"]))

    def test_run_with_a_dead_agent_token_points_at_load_and_does_not_start(self):
        self.moshi.agent_tokens[self.token]["revoked"] = True
        code, _, err, started = self.run_cli("scout")
        self.assertEqual(code, 1)
        self.assertIn("moshi-cockpit agent load scout", err)
        self.assertEqual(len(err.strip().splitlines()), 1)
        started.assert_not_called()
        self.assert_no_secret(err)

    def test_run_with_a_revoked_device_key_asks_for_an_approval(self):
        self.moshi.revoke_installation(self.installation["id"])
        code, _, _, started = self.run_cli("scout")
        self.assertEqual(code, 3)
        started.assert_not_called()

    def test_run_with_a_legacy_token_starts_without_activation(self):
        legacy = self.moshi.add_agent("elder")
        self.connected(agents=[{"name": "elder", "harness": "codex"}])
        self.fake.entries[("moshi-elder", "elder")] = legacy
        code, out, err, started = self.run_cli("elder")
        self.assertEqual((code, out, err), (0, "", ""))
        started.assert_called_once()

    def test_a_missing_binary_takes_nothing_over(self):
        self.moshi.agent("scout")["active_installation_id"] = self.other["id"]
        with mock.patch.object(launch.shutil, "which", return_value=None), \
                mock.patch.object(launch, "execute") as started, mock.patch("cockpit.cli._on_terminal", return_value=False):
            code, out, err = self.cli("run", "scout", "--take-over")
        self.assertEqual((code, out), (1, ""))
        self.assertIn("codex is not found on your PATH", err)
        started.assert_not_called()
        self.assertEqual(self.moshi.calls("POST", "/api/agent/activate"), [])
        self.assertEqual(self.moshi.requests, [])  # moshi was not even asked
        self.assertEqual(self.holder(), self.other["id"])

    def test_on_a_terminal_run_asks_before_it_takes_over(self):
        self.moshi.agent("scout")["active_installation_id"] = self.other["id"]
        code, out, err, started = self.run_cli("scout", terminal=True, answers=["y"])
        self.assertEqual((code, out), (0, ""), err)
        self.assertEqual(self.prompts, ["Running on Home Mac. Take over here? [y/N] "])
        self.assertEqual(self.holder(), self.installation["id"])
        started.assert_called_once()

    def test_on_a_terminal_anything_but_yes_starts_nothing(self):
        self.moshi.agent("scout")["active_installation_id"] = self.other["id"]
        for answer in ("", "n", "later"):
            with self.subTest(answer=answer):
                code, out, _, started = self.run_cli("scout", terminal=True, answers=[answer])
                self.assertEqual((code, out), (4, ""))  # no JSON: a person answered, not the panel
                started.assert_not_called()
                self.assertEqual(self.holder(), self.other["id"])
        self.assertEqual(self.moshi.calls("POST", "/api/agent/activate"), [])

    def test_a_failure_on_a_terminal_waits_for_enter(self):
        self.moshi.agent_tokens[self.token]["revoked"] = True
        code, _, err, started = self.run_cli("scout", terminal=True, answers=[""])
        self.assertEqual(code, 1)
        self.assertIn("moshi-cockpit agent load scout", err)
        self.assertEqual(self.prompts, ["\nPress Enter to close this tab. "])
        started.assert_not_called()

    def test_a_missing_approval_on_a_terminal_exits_3_after_enter(self):
        self.moshi.revoke_installation(self.installation["id"])
        code, _, _, started = self.run_cli("scout", terminal=True, answers=[""])
        self.assertEqual((code, self.prompts), (3, ["\nPress Enter to close this tab. "]))
        started.assert_not_called()

    def test_run_without_a_moshi_token_asks_moshi_nothing(self):
        self.connected(agents=[{"name": "solo", "harness": "codex"}])
        code, _, err, started = self.run_cli("solo")
        self.assertEqual(code, 0, err)
        self.assertEqual(self.moshi.requests, [])
        started.assert_called_once()


if __name__ == "__main__":
    unittest.main()
