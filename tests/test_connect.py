"""`moshi-cockpit connect`, the first sign-in of a Mac. Fake keychain, no network."""

import json
import unittest
from unittest import mock

from cockpit import admin, bots, cli, connect, register, status, tokens
from cockpit.config import load_config, origin, save_servers
from cockpit.secrets import KeychainError
from cockpit.paths import keychain_service
from cockpit.registry import Agent, save_agents
from tests.helpers import MOSHI, OCELLIO, configure
from tests.test_agents_doctor import context

RT = "rt_" + "a" * 26 + "_" + "b" * 43
OCELLIO_TOKEN = "ocellio-token-1234"


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
        with mock.patch.object(bots, "add", return_value=["cockpit angelegt"]) as add:
            notes = connect.run(self.ctx, self.answers(*values))
        self.add = add
        return notes

    def exists(self, service, account):
        return self.ctx.keychain.exists(service, account)

    def test_it_says_what_it_needs_and_asks_hidden_one_after_the_other(self):
        self.run_connect(RT, OCELLIO_TOKEN)
        self.assertIn("zwei Tokens", self.said[0])
        self.assertIn("Admin", self.said[0])
        self.assertIn("ocellio-Konto", self.said[0])
        self.assertEqual(len(self.prompts), 2)
        self.assertIn("Registrier-Token", self.prompts[0])
        self.assertIn("ocellio-Token", self.prompts[1])
        self.assertTrue(all("unsichtbar" in p for p in self.prompts))

    def test_tokens_land_in_the_keychain_bound_to_their_servers(self):
        self.run_connect(RT, OCELLIO_TOKEN)
        self.assertTrue(register.has_register_token(self.ctx.keychain))
        self.assertTrue(self.exists("ocellio-cockpit", "cockpit"))
        self.assertEqual(tokens.bound_origin(self.ctx.paths, tokens.REGISTER), origin(MOSHI))
        self.assertEqual(tokens.bound_origin(self.ctx.paths, tokens.COCKPIT_OCELLIO), origin(OCELLIO))

    def test_no_token_is_ever_shown(self):
        notes = self.run_connect(RT, OCELLIO_TOKEN)
        text = " ".join(self.said + notes + self.prompts)
        self.assertNotIn(RT, text)
        self.assertNotIn(OCELLIO_TOKEN, text)

    def test_it_creates_the_cockpit_identity_last(self):
        self.run_connect(RT, OCELLIO_TOKEN)
        self.add.assert_called_once()
        self.assertEqual(self.add.call_args.args[0], "cockpit")

    def test_an_agent_token_is_refused_and_asked_again(self):
        self.run_connect("agent-token-12345", RT, OCELLIO_TOKEN)
        self.assertEqual(len(self.prompts), 3)
        self.assertTrue(any("kein Registrier-Token" in s for s in self.said))

    def test_it_gives_up_after_three_wrong_tokens(self):
        with self.assertRaises(ValueError):
            self.run_connect(*["agent-token-12345"] * 3)
        self.assertFalse(register.has_register_token(self.ctx.keychain))

    def test_three_wrong_tokens_are_three_questions_not_nine(self):
        with self.assertRaises(ValueError):
            self.run_connect(*["x"] * 9)
        self.assertEqual(len(self.prompts), 3)

    def test_a_token_for_another_moshi_is_never_stored(self):
        tokens.bind_identity(self.ctx.paths, self.ctx.backup, tokens.REGISTER, "https://other.example/mcp")
        with self.assertRaises(ValueError):
            self.run_connect(RT, OCELLIO_TOKEN)
        self.assertFalse(register.has_register_token(self.ctx.keychain))

    def test_a_token_for_another_ocellio_is_never_stored(self):
        tokens.bind_identity(self.ctx.paths, self.ctx.backup, tokens.COCKPIT_OCELLIO, "https://other.example/mcp/")
        with self.assertRaises(ValueError):
            self.run_connect(RT, OCELLIO_TOKEN)
        self.assertFalse(self.exists("ocellio-cockpit", "cockpit"))

    def test_what_is_stored_is_said_before_a_later_failure(self):
        with mock.patch.object(bots, "add", side_effect=admin.AdminError("weg")), self.assertRaises(admin.AdminError):
            connect.run(self.ctx, self.answers(RT, OCELLIO_TOKEN))
        self.assertTrue(any("moshi-register" in s for s in self.said))
        self.assertTrue(any("ocellio-cockpit" in s for s in self.said))

    def test_a_taken_name_warns_against_resetting_the_token(self):
        taken = admin.AdminError(admin.NAME_TAKEN, "name_taken", 409)
        with mock.patch.object(bots, "add", side_effect=taken), self.assertRaises(admin.AdminError) as caught:
            connect.run(self.ctx, self.answers(RT, OCELLIO_TOKEN))
        self.assertIn("nicht zurück", str(caught.exception))
        self.assertTrue(str(caught.exception).endswith("."))
        self.assertNotIn("später", str(caught.exception))
        self.assertIn("cockpit", str(caught.exception))
        self.assertEqual(caught.exception.status, 409)

    def test_other_moshi_errors_stay_as_they_are(self):
        with mock.patch.object(bots, "add", side_effect=admin.AdminError("gesperrt", "forbidden", 403)), \
                self.assertRaises(admin.AdminError) as caught:
            connect.run(self.ctx, self.answers(RT, OCELLIO_TOKEN))
        self.assertEqual(str(caught.exception), "gesperrt")

    def test_an_interrupted_run_only_asks_for_what_is_missing(self):
        with self.assertRaises(ValueError):
            self.run_connect(RT, "x", "x", "x")
        self.said.clear()
        self.run_connect(OCELLIO_TOKEN)
        self.assertEqual(len(self.prompts), 1)
        self.assertIn("ocellio", self.prompts[0])
        self.assertIn("noch", self.said[0])

    def test_nothing_is_asked_when_everything_is_there(self):
        self.run_connect(RT, OCELLIO_TOKEN)
        self.said.clear()
        self.run_connect()
        self.assertEqual(self.prompts, [])
        self.assertEqual(self.said, [])

    def test_without_ocellio_only_moshi_is_asked(self):
        save_servers(self.ctx.paths, self.ctx.backup, {"ocellio": ""})
        self.run_connect(RT)
        self.assertEqual(len(self.prompts), 1)
        self.assertNotIn("ocellio", self.said[0])

    def test_without_moshi_it_stops_with_a_sentence(self):
        save_servers(self.ctx.paths, self.ctx.backup, {"moshi": ""})
        with self.assertRaises(ValueError):
            connect.run(self.ctx, self.answers())


class FirstAgentTest(unittest.TestCase):
    """The point of the ocellio token: the first "+ Agent" needs no further question."""

    def setUp(self):
        self.ctx, self.fake = context(self)
        configure(self.ctx.paths)
        self.config = load_config(self.ctx.paths)

    def store(self, url=OCELLIO):
        tokens.bind_identity(self.ctx.paths, self.ctx.backup, tokens.COCKPIT_OCELLIO, url)
        self.ctx.keychain.write("ocellio-cockpit", "cockpit", OCELLIO_TOKEN)

    def test_a_new_agent_copies_the_token_from_connect(self):
        self.store()
        self.assertEqual(bots._person_ocellio_token("research", self.ctx, self.config), (OCELLIO_TOKEN, False))

    def test_the_copy_only_goes_to_the_server_it_was_bound_to(self):
        self.store("https://other.example/mcp/")
        self.assertIsNone(bots._person_ocellio_token("research", self.ctx, self.config))

    def test_without_connect_there_is_nothing_to_copy(self):
        self.assertIsNone(bots._person_ocellio_token("research", self.ctx, self.config))


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

    def test_the_cockpit_identity_ends_it(self):
        configure(self.ctx.paths)
        self.ctx.keychain.write(keychain_service("moshi", "cockpit"), "cockpit", "moshi-token-1234")
        self.assertFalse(self.needs())

    def test_an_existing_agent_identity_ends_it(self):
        configure(self.ctx.paths)
        save_agents(self.ctx.paths, self.ctx.backup, [Agent(name="research", harness="codex")])
        self.assertTrue(self.needs())
        self.ctx.keychain.write(keychain_service("moshi", "research"), "research", "moshi-token-1234")
        self.assertFalse(self.needs())

    def test_a_stored_registration_token_alone_is_not_connected_yet(self):
        configure(self.ctx.paths)
        register.store_register_token(self.ctx.keychain, RT)
        self.assertTrue(self.needs())


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
        code, text, held = self.run_connect(error=ValueError("Name vergeben"), tty=True)
        self.assertEqual(code, 1)
        self.assertIn("Name vergeben", text)
        held.assert_called_once()

    def test_a_network_error_is_a_plain_failure(self):
        code, text, _ = self.run_connect(error=OSError("Netz weg"))
        self.assertEqual(code, 1)
        self.assertIn("Netz weg", text)

    def test_a_failure_says_that_stored_tokens_stay(self):
        register.store_register_token(self.ctx.keychain, RT)
        self.assertIn(connect.KEPT, self.run_connect(error=ValueError("x"))[1])

    def test_with_nothing_stored_it_promises_nothing(self):
        self.assertNotIn(connect.KEPT, self.run_connect(error=ValueError("x"))[1])

    def test_a_taken_name_is_not_worth_another_try(self):
        register.store_register_token(self.ctx.keychain, RT)
        taken = admin.AdminError("vergeben", "name_taken", 409)
        self.assertNotIn(connect.KEPT, self.run_connect(error=taken)[1])

    def test_the_text_status_still_names_the_missing_token(self):
        args = mock.Mock(identity=None, json=False)
        with mock.patch.object(cli, "default_store", return_value=self.ctx.keychain), self.assertRaises(KeychainError) as caught:
            cli.cmd_status(args, self.ctx.paths)
        self.assertIn("moshi-Token", str(caught.exception))

    def test_without_a_terminal_nothing_waits(self):
        _, _, held = self.run_connect()
        held.assert_not_called()


if __name__ == "__main__":
    unittest.main()
