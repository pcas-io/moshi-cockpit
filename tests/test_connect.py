"""`moshi-cockpit connect`, the first sign-in of a Mac. Fake keychain, no network."""

import json
import unittest
from unittest import mock

from cockpit import cli, connect, status, tokens
from cockpit.config import load_config, origin, save_servers
from cockpit.secrets import KeychainError
from cockpit.paths import keychain_service
from cockpit.registry import Agent, save_agents
from tests.helpers import MOSHI, OCELLIO, configure
from tests.test_agents_doctor import context

RT = "rt_" + "a" * 26 + "_" + "b" * 43
OCELLIO_TOKEN = "ocellio-token-1234"
REGISTER_ENTRY = ("moshi-register", "register")
OCELLIO_ENTRY = ("ocellio-default", "default")


class ConnectTest(unittest.TestCase):
    def setUp(self):
        self.ctx, self.fake = context(self)
        configure(self.ctx.paths)
        self.said = []

    def answers(self, *values):
        queue = list(values)
        self.prompts = []

        def ask(prompt):
            self.prompts.append(prompt)
            return queue.pop(0)
        return connect.ConnectIO(ask_secret=ask, say=self.said.append)

    def run_connect(self, *values):
        return connect.run(self.ctx, self.answers(*values))

    def exists(self, service, account):
        return self.ctx.keychain.exists(service, account)

    def test_it_says_what_it_needs_and_asks_hidden_one_after_the_other(self):
        self.run_connect(RT, OCELLIO_TOKEN)
        self.assertIn("two tokens", self.said[0])
        self.assertIn("ocellio account", self.said[0])
        self.assertEqual(len(self.prompts), 2)
        self.assertIn("registration token", self.prompts[0])
        self.assertIn("ocellio token", self.prompts[1])
        self.assertTrue(all("hidden" in p for p in self.prompts))

    def test_tokens_land_in_the_keychain_bound_to_their_servers(self):
        self.run_connect(RT, OCELLIO_TOKEN)
        self.assertEqual(self.fake.entries[REGISTER_ENTRY], RT)
        self.assertTrue(self.ctx.keychain.is_strict(*REGISTER_ENTRY))  # macOS asks on every read
        self.assertEqual(self.fake.entries[OCELLIO_ENTRY], OCELLIO_TOKEN)
        self.assertEqual(tokens.bound_origin(self.ctx.paths, tokens.REGISTER), origin(MOSHI))
        self.assertEqual(tokens.bound_origin(self.ctx.paths, tokens.OCELLIO_DEFAULT), origin(OCELLIO))
        self.assertFalse(self.fake.argv_contains(RT) or self.fake.argv_contains(OCELLIO_TOKEN))

    def test_no_token_is_ever_shown(self):
        notes = self.run_connect(RT, OCELLIO_TOKEN)
        text = " ".join(self.said + notes + self.prompts)
        self.assertNotIn(RT, text)
        self.assertNotIn(OCELLIO_TOKEN, text)

    def test_it_creates_no_moshi_identity_of_its_own(self):
        self.run_connect(RT, OCELLIO_TOKEN)
        self.assertEqual(set(self.fake.entries), {REGISTER_ENTRY, OCELLIO_ENTRY})
        self.assertNotIn("cockpit", json.loads(self.ctx.paths.mesh_config.read_text())["bound"])

    def test_an_agent_token_is_refused_and_asked_again(self):
        self.run_connect("agent-token-12345", RT, OCELLIO_TOKEN)
        self.assertEqual(len(self.prompts), 3)
        self.assertTrue(any("not a registration token" in s for s in self.said))

    def test_it_gives_up_after_three_wrong_tokens(self):
        with self.assertRaises(ValueError):
            self.run_connect(*["agent-token-12345"] * 3)
        self.assertFalse(self.exists(*REGISTER_ENTRY))

    def test_three_wrong_tokens_are_three_questions_not_nine(self):
        with self.assertRaises(ValueError):
            self.run_connect(*["x"] * 9)
        self.assertEqual(len(self.prompts), 3)

    def test_a_token_for_another_moshi_is_never_stored(self):
        tokens.bind_identity(self.ctx.paths, self.ctx.backup, tokens.REGISTER, "https://other.example/mcp")
        with self.assertRaises(ValueError):
            self.run_connect(RT, OCELLIO_TOKEN)
        self.assertFalse(self.exists(*REGISTER_ENTRY))

    def test_a_token_for_another_ocellio_is_never_stored(self):
        tokens.bind_identity(self.ctx.paths, self.ctx.backup, tokens.OCELLIO_DEFAULT, "https://other.example/mcp/")
        with self.assertRaises(ValueError):
            self.run_connect(RT, OCELLIO_TOKEN)
        self.assertFalse(self.exists(*OCELLIO_ENTRY))

    def test_what_is_stored_is_said_before_a_later_failure(self):
        with self.assertRaises(ValueError):
            self.run_connect(RT, "x", "x", "x")
        self.assertTrue(any("moshi-register" in s for s in self.said))

    def test_an_interrupted_run_only_asks_for_what_is_missing(self):
        with self.assertRaises(ValueError):
            self.run_connect(RT, "x", "x", "x")
        self.said.clear()
        self.run_connect(OCELLIO_TOKEN)
        self.assertEqual(len(self.prompts), 1)
        self.assertIn("ocellio", self.prompts[0])
        self.assertIn("still", self.said[0])

    def test_nothing_is_asked_when_everything_is_there(self):
        self.run_connect(RT, OCELLIO_TOKEN)
        self.said.clear()
        notes = self.run_connect()
        self.assertEqual(self.prompts, [])
        self.assertEqual(self.said, [])
        self.assertIn("nothing to do", notes[0])

    def test_without_ocellio_only_moshi_is_asked(self):
        save_servers(self.ctx.paths, self.ctx.backup, {"ocellio": ""})
        self.run_connect(RT)
        self.assertEqual(len(self.prompts), 1)
        self.assertNotIn("ocellio", self.said[0])

    def test_without_moshi_it_stops_with_a_sentence(self):
        save_servers(self.ctx.paths, self.ctx.backup, {"moshi": ""})
        with self.assertRaises(ValueError):
            connect.run(self.ctx, self.answers())


class NeedsConnectTest(unittest.TestCase):
    def setUp(self):
        self.ctx, self.fake = context(self)

    def needs(self):
        return connect.needs_connect(self.ctx.paths, load_config(self.ctx.paths), self.ctx.keychain)

    def test_a_new_mac_with_moshi_needs_it(self):
        configure(self.ctx.paths)
        self.assertTrue(self.needs())

    def test_without_moshi_the_cockpit_stays_local_and_silent(self):
        self.assertFalse(self.needs())

    def test_a_former_cockpit_identity_does_not_count(self):
        configure(self.ctx.paths)
        self.ctx.keychain.write(keychain_service("moshi", "cockpit"), "cockpit", "moshi-token-1234")
        self.assertTrue(self.needs())

    def test_an_existing_agent_identity_ends_it(self):
        configure(self.ctx.paths)
        save_agents(self.ctx.paths, self.ctx.backup, [Agent(name="research", harness="codex")])
        self.assertTrue(self.needs())
        self.ctx.keychain.write(keychain_service("moshi", "research"), "research", "moshi-token-1234")
        self.assertFalse(self.needs())

    def test_a_stored_registration_token_ends_it(self):
        configure(self.ctx.paths)
        self.ctx.keychain.write(*REGISTER_ENTRY, RT, strict=True)
        self.assertFalse(self.needs())


class CommandLineTest(unittest.TestCase):
    def setUp(self):
        self.ctx, self.fake = context(self)
        configure(self.ctx.paths)

    def status(self):
        args = mock.Mock(identity=None, json=True)
        with mock.patch.object(cli, "default_store", return_value=self.ctx.keychain), mock.patch("builtins.print") as printed:
            code = cli.cmd_status(args, self.ctx.paths)
        return code, json.loads(printed.call_args.args[0])

    def test_the_panel_gets_data_not_an_error_on_a_new_mac(self):
        code, data = self.status()
        self.assertEqual(code, 0)
        self.assertIs(data["needs_connect"], True)
        self.assertEqual(data["identity"], "")
        self.assertEqual(data["agents"], [])

    def test_after_connect_without_agents_the_panel_gets_the_local_view(self):
        self.ctx.keychain.write(*REGISTER_ENTRY, RT, strict=True)  # what a successful connect leaves
        code, data = self.status()
        self.assertEqual(code, 0)
        self.assertIs(data["needs_connect"], False)
        self.assertEqual(data["identity"], "")
        self.assertEqual(data["agents"], [])
        self.assertEqual(data["servers"], {"moshi": True, "ocellio": True})

    def test_the_payload_defaults_to_connected(self):
        self.assertIs(json.loads(status.as_json({}, "x"))["needs_connect"], False)

    def run_connect(self, result=None, error=None, tty=False):
        run = mock.Mock(return_value=result or ["ok"], side_effect=error)
        with mock.patch.object(cli.connect, "run", run), mock.patch.object(cli, "_context", return_value=self.ctx), \
                mock.patch("builtins.print") as printed, mock.patch("sys.stdin.isatty", return_value=tty), \
                mock.patch("builtins.input") as held:
            code = cli.cmd_connect(mock.Mock(), self.ctx.paths)
        return code, " ".join(str(c.args[0]) for c in printed.call_args_list), held

    def test_success_points_back_to_herdr(self):
        code, text, _ = self.run_connect()
        self.assertEqual(code, 0)
        self.assertIn("+ Agent", text)

    def test_a_failure_stays_readable_until_enter(self):
        code, text, held = self.run_connect(error=ValueError("Name taken"), tty=True)
        self.assertEqual(code, 1)
        self.assertIn("Name taken", text)
        held.assert_called_once()

    def test_a_network_error_is_a_plain_failure(self):
        code, text, _ = self.run_connect(error=OSError("network down"))
        self.assertEqual(code, 1)
        self.assertIn("network down", text)

    def test_a_failure_says_that_stored_tokens_stay(self):
        self.ctx.keychain.write(*REGISTER_ENTRY, RT, strict=True)
        self.assertIn(connect.KEPT, self.run_connect(error=ValueError("x"))[1])

    def test_with_nothing_stored_it_promises_nothing(self):
        self.assertNotIn(connect.KEPT, self.run_connect(error=ValueError("x"))[1])

    def test_the_text_status_still_names_the_missing_token(self):
        args = mock.Mock(identity=None, json=False)
        with mock.patch.object(cli, "default_store", return_value=self.ctx.keychain), self.assertRaises(KeychainError) as caught:
            cli.cmd_status(args, self.ctx.paths)
        self.assertIn("moshi token", str(caught.exception))

    def test_without_a_terminal_nothing_waits(self):
        _, _, held = self.run_connect()
        held.assert_not_called()


if __name__ == "__main__":
    unittest.main()
