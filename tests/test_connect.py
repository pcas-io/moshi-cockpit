"""`moshi-cockpit connect`: one browser approval against FakeMoshi. Fake keychain, no real browser, no waiting."""

import dataclasses
import io
import json
import os
import re
import unittest
from contextlib import redirect_stderr
from datetime import datetime, timedelta, timezone
from unittest import mock
from urllib.parse import parse_qs, urlsplit

from cockpit import cli, connect, registry, status, tokens
from cockpit.config import load_config, origin, save_servers
from cockpit.moshi_api import MoshiApi, MoshiError, parse_timestamp
from cockpit.paths import keychain_service
from cockpit.registry import Agent, save_agents
from cockpit.secrets import KeychainError
from tests.fake_moshi import serve
from tests.helpers import MOSHI, OCELLIO, configure
from tests.test_agents_doctor import context

OCELLIO_TOKEN = "ocellio-token-1234"
DEVICE_ENTRY = ("moshi-device", "device")
REGISTER_ENTRY = ("moshi-register", "register")
OCELLIO_ENTRY = ("ocellio-default", "default")
RT = "rt_" + "a" * 26 + "_" + "b" * 43


class Served:
    """FakeMoshi through MoshiApi, with its approval page moved to `link` (FakeMoshi runs on 127.0.0.1, the
    config needs https) and `failures` polls lost as if the network were down."""

    def __init__(self, api, link, failures=0):
        self.api, self.link, self.failures = api, link, failures

    def device_authorize(self, label, device_key=None):
        start = self.api.device_authorize(label, device_key=device_key)
        served = origin(start.verification_uri)
        return dataclasses.replace(start, verification_uri=start.verification_uri.replace(served, self.link, 1),
                                   verification_uri_complete=start.verification_uri_complete.replace(served, self.link, 1))

    def device_token(self, device_code):
        if self.failures:
            self.failures -= 1
            raise MoshiError("unreachable", 0)
        return self.api.device_token(device_code)


class Failing:
    def __init__(self, error):
        self.error = error

    def device_authorize(self, *args, **kwargs):
        raise self.error


class ConnectTest(unittest.TestCase):
    def setUp(self):
        self.ctx, self.fake = context(self)
        configure(self.ctx.paths)
        self.moshi = serve(self)
        self.api = self.served()
        self.said, self.prompts, self.opened, self.slept = [], [], [], []
        self.answers = [OCELLIO_TOKEN]
        self.decision = self.moshi.approve  # what the person does in the browser

    def served(self, link=origin(MOSHI), failures=0):
        return Served(MoshiApi(self.moshi.url), link, failures)

    def ask(self, prompt):
        self.prompts.append(prompt)
        return self.answers.pop(0)

    def browser(self, url):
        self.opened.append(url)
        self.decision(parse_qs(urlsplit(url).query)["code"][0])
        return True

    def run_connect(self, api=None, browser=None, sleep=None):
        io = connect.ConnectIO(ask_secret=self.ask, say=self.said.append)
        return connect.run(self.ctx, io, api or self.api, opener=browser or self.browser,
                           sleep=sleep or self.slept.append, label="test-mac")

    def entry(self, key):
        return self.fake.entries.get(key)

    def live_registration(self):
        return [t for t, e in self.moshi.registration_tokens.items() if not e["revoked"]]

    def expire_locally(self):
        data = json.loads(self.ctx.paths.mesh_config.read_text())
        data[connect.EXPIRES_KEY] = "2020-01-01T00:00:00.000Z"
        self.ctx.paths.mesh_config.write_text(json.dumps(data))

    def text(self):
        return "\n".join(self.said + self.prompts)

    def test_connect_stores_device_key_and_registration_token_after_approval(self):
        self.assertEqual(self.run_connect(), 0)
        installation = next(iter(self.moshi.installations.values()))
        self.assertEqual(self.entry(DEVICE_ENTRY), installation["device_key"])
        self.assertEqual([self.entry(REGISTER_ENTRY)], self.live_registration())
        self.assertTrue(self.ctx.keychain.is_strict(*REGISTER_ENTRY))   # macOS asks on every read
        self.assertFalse(self.ctx.keychain.is_strict(*DEVICE_ENTRY))   # the sidebar reads it without asking
        expires = self.moshi.registration_tokens[self.entry(REGISTER_ENTRY)]["expires_at"]
        saved = json.loads(self.ctx.paths.mesh_config.read_text())[connect.EXPIRES_KEY]
        self.assertEqual(parse_timestamp(saved), expires.replace(microsecond=expires.microsecond // 1000 * 1000))
        for identity in (tokens.DEVICE, tokens.REGISTER):
            self.assertEqual(tokens.bound_origin(self.ctx.paths, identity), origin(MOSHI))

    def test_connect_says_each_step(self):
        self.run_connect()
        start = self.moshi.calls("POST", "/api/device/authorize")[0]
        self.assertEqual(start["body"], {"label": "Herdr on test-mac"})
        self.assertIsNone(start["auth"])
        code = next(iter(self.moshi.flows.values()))["user_code"]
        shown = f"{code[:4]}-{code[4:]}"
        self.assertEqual(self.said[:2], [
            "Connecting this Herdr to moshi.",
            f"Confirm in your browser. If it opens on another device, go to {origin(MOSHI)}/link and enter {shown}."])
        self.assertEqual(self.opened, [f"{origin(MOSHI)}/link?code={shown}"])
        self.assertIn('Connected as "Herdr on test-mac".', self.said)
        self.assertEqual(self.said[-1], connect.DONE)
        self.assertEqual(self.slept, [5])  # moshi's interval before the first poll

    def test_no_token_is_ever_shown_or_put_into_argv(self):
        self.run_connect()
        secrets = [self.entry(DEVICE_ENTRY), self.entry(REGISTER_ENTRY), OCELLIO_TOKEN]
        for secret in secrets:
            self.assertNotIn(secret, self.text())
            self.assertFalse(self.fake.argv_contains(secret))

    def test_connect_with_an_expired_registration_token_renews_it_and_keeps_the_device_key(self):
        self.run_connect()
        installation_id = next(iter(self.moshi.installations))
        agent_token = self.moshi.add_agent("research", installation_id=installation_id)
        device_key, old_rt = self.entry(DEVICE_ENTRY), self.entry(REGISTER_ENTRY)
        self.expire_locally()
        self.said.clear()
        self.assertEqual(self.run_connect(), 0)
        renewal = self.moshi.calls("POST", "/api/device/authorize")[-1]
        self.assertEqual(renewal["auth"], f"Bearer {device_key}")
        self.assertEqual(len(self.moshi.installations), 1)
        self.assertEqual(self.entry(DEVICE_ENTRY), device_key)
        self.assertNotEqual(self.entry(REGISTER_ENTRY), old_rt)
        self.assertEqual([self.entry(REGISTER_ENTRY)], self.live_registration())
        self.assertTrue(self.ctx.keychain.is_strict(*REGISTER_ENTRY))
        self.assertFalse(self.moshi.agent_tokens[agent_token]["revoked"])
        self.assertTrue(connect.approval_valid(self.ctx.paths, self.ctx.keychain, datetime.now(timezone.utc)))
        self.assertIn('Connected as "Herdr on test-mac".', self.said)

    def test_a_revoked_device_key_connects_anew(self):
        self.run_connect()
        old_key = self.entry(DEVICE_ENTRY)
        self.moshi.revoke_installation(next(iter(self.moshi.installations)))
        self.assertEqual(self.run_connect(), 0)
        self.assertEqual(len(self.moshi.installations), 2)
        self.assertNotEqual(self.entry(DEVICE_ENTRY), old_key)
        self.assertEqual(self.moshi.live_installation(self.entry(DEVICE_ENTRY))["label"], "Herdr on test-mac")
        self.assertEqual([self.entry(REGISTER_ENTRY)], self.live_registration())

    def test_connect_honours_slow_down(self):
        self.decision = lambda code: None
        self.moshi.slow_down = 2

        def sleep(seconds):
            self.slept.append(seconds)
            if len(self.slept) == 3:
                self.moshi.approve(next(iter(self.moshi.flows.values()))["user_code"])
        self.assertEqual(self.run_connect(sleep=sleep), 0)
        self.assertEqual(self.slept, [5, 10, 15])
        self.assertIsNotNone(self.entry(REGISTER_ENTRY))

    def test_connect_stops_waiting_at_expires_in(self):
        self.decision = lambda code: None
        self.assertEqual(self.run_connect(), 1)
        self.assertEqual(sum(self.slept), 600)
        self.assertEqual(len(self.moshi.calls("POST", "/api/device/token")), 120)
        self.assertEqual(self.said[-1], "The code expired. Run connect again.")

    def test_connect_reports_an_expired_code(self):
        self.decision = self.moshi.expire
        self.assertEqual(self.run_connect(), 1)
        self.assertEqual(self.said[-1], "The code expired. Run connect again.")
        self.assertEqual(self.fake.entries, {})
        self.assertEqual(self.prompts, [])

    def test_connect_reports_a_denied_request(self):
        self.decision = self.moshi.deny
        self.assertEqual(self.run_connect(), 1)
        self.assertEqual(self.said[-1], "Connection was denied in moshi.")
        self.assertEqual(self.fake.entries, {})

    def test_a_network_failure_while_polling_is_retried_with_the_same_interval(self):
        self.assertEqual(self.run_connect(api=self.served(failures=3)), 0)
        self.assertEqual(self.slept, [5, 5, 5, 5])
        self.assertIsNotNone(self.entry(REGISTER_ENTRY))

    def test_a_network_failure_until_the_expiry_ends_with_one_clear_line(self):
        self.assertEqual(self.run_connect(api=self.served(failures=10 ** 6)), 1)
        self.assertEqual(sum(self.slept), 600)
        self.assertEqual(self.said[-1], connect.LOST)
        self.assertEqual(self.fake.entries, {})

    def test_moshi_unreachable_at_the_start_is_one_clear_line(self):
        self.assertEqual(self.run_connect(api=MoshiApi("http://127.0.0.1:1", timeout=2)), 1)
        self.assertEqual(self.said[-1], connect.UNREACHABLE.format(origin=origin(MOSHI)))
        self.assertEqual(self.opened, [])

    def test_connect_asks_for_the_ocellio_token_only_once(self):
        self.assertEqual(self.run_connect(), 0)
        self.assertEqual(self.prompts, ["Your ocellio token (input hidden, stays in the Keychain): "])
        self.assertEqual(self.entry(OCELLIO_ENTRY), OCELLIO_TOKEN)
        self.assertFalse(self.ctx.keychain.is_strict(*OCELLIO_ENTRY))
        self.assertEqual(tokens.bound_origin(self.ctx.paths, tokens.OCELLIO_DEFAULT), origin(OCELLIO))
        self.expire_locally()
        self.assertEqual(self.run_connect(), 0)
        self.assertEqual(len(self.prompts), 1)

    def test_a_wrong_ocellio_token_is_not_asked_again_and_not_stored(self):
        self.answers = ["not a token"]
        self.assertEqual(self.run_connect(), 1)
        self.assertEqual(len(self.prompts), 1)
        self.assertNotIn(OCELLIO_ENTRY, self.fake.entries)
        self.assertIsNotNone(self.entry(REGISTER_ENTRY))  # what moshi granted stays
        self.assertEqual(self.said[-1], connect.NO_OCELLIO)

    def test_without_ocellio_nothing_is_asked(self):
        save_servers(self.ctx.paths, self.ctx.backup, {"ocellio": ""})
        self.assertEqual(self.run_connect(), 0)
        self.assertEqual(self.prompts, [])

    def test_without_moshi_it_stops_with_a_sentence(self):
        save_servers(self.ctx.paths, self.ctx.backup, {"moshi": ""})
        with self.assertRaises(ValueError):
            self.run_connect()
        self.assertEqual(self.moshi.requests, [])

    def test_a_herdr_connected_to_another_moshi_sends_nothing_and_says_what_to_do(self):
        for identity in (tokens.DEVICE, tokens.REGISTER):
            with self.subTest(identity=identity):
                configure(self.ctx.paths)
                self.ctx.keychain.write(*DEVICE_ENTRY, "dk_other-device-key-1234")
                tokens.bind_identity(self.ctx.paths, self.ctx.backup, identity, "https://other.example/mcp")
                with self.assertRaises(ValueError) as caught:
                    self.run_connect()
                self.assertEqual(str(caught.exception), connect.ANOTHER)
                self.assertEqual(self.moshi.requests, [])
                self.assertEqual(self.entry(DEVICE_ENTRY), "dk_other-device-key-1234")

    def test_a_device_key_bound_to_no_server_is_never_sent_and_connects_anew(self):
        self.ctx.keychain.write(*DEVICE_ENTRY, "dk_unbound-device-key-1234")
        self.assertEqual(self.run_connect(), 0)
        self.assertIn(connect.UNKNOWN, self.said)
        self.assertIsNone(self.moshi.calls("POST", "/api/device/authorize")[0]["auth"])
        self.assertFalse(any("dk_unbound" in str(r) for r in self.moshi.requests))
        self.assertEqual(self.moshi.live_installation(self.entry(DEVICE_ENTRY))["label"], "Herdr on test-mac")
        self.assertEqual(tokens.bound_origin(self.ctx.paths, tokens.DEVICE), origin(MOSHI))

    def test_an_approval_page_of_another_origin_is_never_opened(self):
        self.decision = lambda code: None

        def sleep(seconds):
            self.slept.append(seconds)
            self.moshi.approve(next(iter(self.moshi.flows.values()))["user_code"])
        self.assertEqual(self.run_connect(api=self.served(link=self.moshi.origin), sleep=sleep), 0)
        self.assertEqual(self.opened, [])
        code = next(iter(self.moshi.flows.values()))["user_code"]
        self.assertEqual(self.said[1], f"Open {self.moshi.origin}/link in your browser and enter {code[:4]}-{code[4:]}.")
        self.assertIsNotNone(self.entry(REGISTER_ENTRY))

    def test_a_server_error_at_the_start_is_not_called_unreachable(self):
        self.assertEqual(self.run_connect(api=Failing(MoshiError("http_error", 503))), 1)
        self.assertEqual(self.said[-1], "moshi answered with an error (503). Try again later.")
        with self.assertRaises(MoshiError):
            self.run_connect(api=Failing(MoshiError("too_many_requests", 429)))

    def test_a_valid_approval_with_only_the_ocellio_token_missing_skips_the_browser(self):
        self.run_connect()
        del self.fake.entries[OCELLIO_ENTRY]
        self.said.clear()
        self.answers = [OCELLIO_TOKEN]
        self.assertEqual(self.run_connect(), 0)
        self.assertEqual(len(self.moshi.calls("POST", "/api/device/authorize")), 1)
        self.assertEqual(len(self.opened), 1)
        self.assertEqual(self.said, [connect.ALREADY, connect.DONE])
        self.assertEqual(len(self.prompts), 2)
        self.assertEqual(self.entry(OCELLIO_ENTRY), OCELLIO_TOKEN)

    def test_the_label_defaults_to_the_host_name(self):
        io = connect.ConnectIO(ask_secret=self.ask, say=self.said.append)
        with mock.patch.object(connect.socket, "gethostname", return_value="studio"):
            connect.run(self.ctx, io, self.api, opener=self.browser, sleep=self.slept.append)
        self.assertEqual(self.moshi.calls("POST", "/api/device/authorize")[0]["body"], {"label": "Herdr on studio"})

    def run_headless(self, label="devbox", decision=None):
        """A Linux machine: nothing is opened, the person approves elsewhere while connect waits."""
        decide = decision or self.moshi.approve

        def sleep(seconds):
            self.slept.append(seconds)
            decide(next(iter(self.moshi.flows.values()))["user_code"])
        io = connect.ConnectIO(ask_secret=self.ask, say=self.said.append)
        return connect.run(self.ctx, io, self.api, opener=self.opened.append, sleep=sleep, label=label, headless=True)

    def test_headless_prints_link_and_code_and_never_opens(self):
        self.assertEqual(self.run_headless(), 0)
        self.assertEqual(self.opened, [])
        code = next(iter(self.moshi.flows.values()))["user_code"]
        self.assertIn(connect.HEADLESS_OPEN.format(link=f"{origin(MOSHI)}/link", code=f"{code[:4]}-{code[4:]}"),
                      self.said)
        self.assertEqual(self.moshi.calls("POST", "/api/device/authorize")[0]["body"], {"label": "devbox"})
        self.assertEqual(self.said[-1], connect.DONE_HEADLESS)
        self.assertIsNotNone(self.entry(REGISTER_ENTRY))

    def test_headless_label_defaults_to_the_host_name(self):
        with mock.patch.object(connect.socket, "gethostname", return_value="build-1"):
            self.assertEqual(self.run_headless(label=None), 0)
        self.assertEqual(self.moshi.calls("POST", "/api/device/authorize")[0]["body"], {"label": "build-1"})

    def test_headless_denied_and_expired_keep_their_exit_codes(self):
        for decision, sentence in ((self.moshi.deny, connect.DENIED), (self.moshi.expire, connect.EXPIRED)):
            with self.subTest(sentence=sentence):
                self.said.clear()
                self.assertEqual(self.run_headless(decision=decision), 1)
                self.assertEqual(self.said[-1], sentence)
                self.assertEqual(self.opened, [])
        self.assertEqual(self.fake.entries, {})

    def test_headless_ocellio_prompt_names_the_store(self):
        self.assertEqual(self.run_headless(), 0)
        self.assertEqual(self.prompts, [connect.OCELLIO_PROMPT_HEADLESS.format(store=self.ctx.keychain.name)])
        self.assertEqual(self.entry(OCELLIO_ENTRY), OCELLIO_TOKEN)

    def test_headless_says_this_installation_and_names_its_store(self):
        no_herdr = re.compile(r"this Herdr|the Keychain", re.IGNORECASE)
        self.ctx.keychain.write(*DEVICE_ENTRY, "dk_unbound-device-key-1234")
        self.assertEqual(self.run_headless(), 0)
        self.assertIn("Connecting this installation to moshi.", self.said)
        self.assertIn("This installation's earlier connection is unknown here; connecting it again.", self.said)
        self.said.clear()
        del self.fake.entries[OCELLIO_ENTRY]
        self.answers = [OCELLIO_TOKEN]
        self.assertEqual(self.run_headless(), 0)
        self.assertEqual(self.said[0], "This installation is connected to moshi already.")
        self.assertFalse([line for line in self.said + self.prompts if no_herdr.search(line)])
        configure(self.ctx.paths)
        tokens.bind_identity(self.ctx.paths, self.ctx.backup, tokens.DEVICE, "https://other.example/mcp")
        with self.assertRaises(ValueError) as caught:
            self.run_headless()
        self.assertEqual(str(caught.exception),
                         f"This installation is connected to another moshi. Run moshi-cockpit uninstall or remove the "
                         f"old connection in {self.ctx.keychain.name}, then connect again.")

    def test_headless_a_forgotten_device_key_says_this_installation(self):
        self.run_connect()
        self.moshi.revoke_installation(next(iter(self.moshi.installations)))
        self.said.clear()
        newest = lambda _code: self.moshi.approve(list(self.moshi.flows.values())[-1]["user_code"])  # noqa: E731
        self.assertEqual(self.run_headless(decision=newest), 0)
        self.assertIn("moshi no longer knows this installation; it is connected as a new one.", self.said)

    def test_text_from_moshi_reaches_the_terminal_without_control_characters(self):
        self.run_connect()
        installation = next(iter(self.moshi.installations.values()))
        installation["label"] = "x\x1b]0;evil\x07"
        self.expire_locally()
        self.said.clear()
        self.run_connect()
        self.assertFalse(any("\x1b" in line or "\x07" in line for line in self.said))


class OpenBrowserTest(unittest.TestCase):
    def test_macos_opens_the_url_as_one_argument(self):
        with mock.patch.object(connect.platform, "system", return_value="Darwin"), \
                mock.patch.object(connect.subprocess, "run") as run:
            run.return_value.returncode = 0
            self.assertTrue(connect.open_browser("https://moshi.example.test/link?code=BCDF-GHJK"))
        self.assertEqual(run.call_args.args[0], ["/usr/bin/open", "https://moshi.example.test/link?code=BCDF-GHJK"])

    def test_elsewhere_the_standard_browser_module_opens_it(self):
        with mock.patch.object(connect.platform, "system", return_value="Linux"), \
                mock.patch.object(connect.webbrowser, "open", return_value=True) as opened:
            self.assertTrue(connect.open_browser("https://moshi.example.test/link"))
        opened.assert_called_once_with("https://moshi.example.test/link")

    def test_only_https_is_ever_opened(self):
        with mock.patch.object(connect.subprocess, "run") as run, mock.patch.object(connect.webbrowser, "open") as web:
            for url in ("file:///Applications/Calculator.app", "-a Terminal", "http://moshi.example.test/link"):
                self.assertFalse(connect.open_browser(url))
        run.assert_not_called()
        web.assert_not_called()


class NeedsConnectTest(unittest.TestCase):
    def setUp(self):
        self.ctx, self.fake = context(self)

    def needs(self):
        return connect.needs_connect(self.ctx.paths, load_config(self.ctx.paths), self.ctx.keychain)

    def test_a_new_herdr_with_moshi_needs_it(self):
        configure(self.ctx.paths)
        self.assertTrue(self.needs())

    def test_without_moshi_the_cockpit_stays_local_and_silent(self):
        self.assertFalse(self.needs())

    def test_needs_connect_is_false_once_a_device_key_exists(self):
        configure(self.ctx.paths)
        self.ctx.keychain.write(*DEVICE_ENTRY, "dk_device-key-1234")
        self.assertFalse(self.needs())

    def test_agent_tokens_or_a_registration_token_alone_do_not_end_it(self):
        configure(self.ctx.paths)
        save_agents(self.ctx.paths, self.ctx.backup, [Agent(name="research", harness="codex")])
        self.ctx.keychain.write(keychain_service("moshi", "research"), "research", "moshi-token-1234")
        self.ctx.keychain.write(*REGISTER_ENTRY, RT, strict=True)
        self.assertTrue(self.needs())


class ApprovalValidTest(unittest.TestCase):
    def setUp(self):
        self.ctx, self.fake = context(self)
        configure(self.ctx.paths)
        self.now = datetime(2026, 11, 16, 12, 0, tzinfo=timezone.utc)

    def expires(self, value):
        data = json.loads(self.ctx.paths.mesh_config.read_text())
        data[connect.EXPIRES_KEY] = value
        self.ctx.paths.mesh_config.write_text(json.dumps(data))

    def valid(self, now=None):
        return connect.approval_valid(self.ctx.paths, self.ctx.keychain, now or self.now)

    def test_approval_valid_is_false_after_register_expires_at(self):
        self.ctx.keychain.write(*REGISTER_ENTRY, RT, strict=True)
        self.expires("2026-11-16T12:00:00.000Z")
        self.assertTrue(self.valid(self.now - timedelta(seconds=1)))
        self.assertFalse(self.valid(self.now))
        self.assertFalse(self.valid(self.now + timedelta(hours=1)))

    def test_an_offset_is_compared_as_the_same_moment(self):
        self.ctx.keychain.write(*REGISTER_ENTRY, RT, strict=True)
        self.expires("2026-11-16T13:30:00+01:00")  # 12:30 UTC
        self.assertTrue(self.valid(datetime(2026, 11, 16, 12, 29, tzinfo=timezone.utc)))
        self.assertFalse(self.valid(datetime(2026, 11, 16, 12, 31, tzinfo=timezone.utc)))

    def test_a_naive_now_counts_as_utc(self):
        self.ctx.keychain.write(*REGISTER_ENTRY, RT, strict=True)
        self.expires("2026-11-16T12:00:00.000Z")
        self.assertTrue(self.valid(datetime(2026, 11, 16, 11, 59)))
        self.assertFalse(self.valid(datetime(2026, 11, 16, 12, 1)))

    def test_without_a_registration_token_it_is_false(self):
        self.expires("2099-01-01T00:00:00Z")
        self.assertFalse(self.valid())

    def test_a_missing_or_broken_expiry_is_false(self):
        self.ctx.keychain.write(*REGISTER_ENTRY, RT, strict=True)
        self.assertFalse(self.valid())
        for broken in ("tomorrow", 12, None, ""):
            self.expires(broken)
            self.assertFalse(self.valid(), broken)


class ReservedNamesTest(unittest.TestCase):
    def test_device_and_default_are_reserved_like_their_keychain_entries(self):
        for name in ("device", "Default", "register", "cockpit"):
            with self.subTest(name=name), self.assertRaises(ValueError):
                registry.validate_name(name)


class CommandLineTest(unittest.TestCase):
    def setUp(self):
        self.ctx, self.fake = context(self)
        configure(self.ctx.paths)

    def status(self):
        args = mock.Mock(json=True)
        with mock.patch.object(cli, "default_store", return_value=self.ctx.keychain), mock.patch("builtins.print") as printed:
            code = cli.cmd_status(args, self.ctx.paths)
        return code, json.loads(printed.call_args.args[0])

    def test_the_panel_gets_data_not_an_error_on_a_new_herdr(self):
        code, data = self.status()
        self.assertEqual(code, 0)
        self.assertIs(data["needs_connect"], True)
        self.assertEqual(data["agents"], [])

    def test_after_connect_without_agents_the_panel_gets_the_local_view(self):
        self.ctx.keychain.write(*DEVICE_ENTRY, "dk_device-key-1234")  # what a successful connect leaves
        self.ctx.keychain.write(*REGISTER_ENTRY, RT, strict=True)
        code, data = self.status()
        self.assertEqual(code, 0)
        self.assertIs(data["needs_connect"], False)
        self.assertIs(data["approval_valid"], False)  # no register_expires_at written in this test
        self.assertEqual(data["agents"], [])
        self.assertEqual(data["servers"], {"moshi": True, "ocellio": True})

    def test_the_payload_defaults_to_connected(self):
        data = json.loads(status.as_json(status.Mesh()))
        self.assertEqual((data["needs_connect"], data["disconnected"], data["unreachable"]), (False, False, False))

    def run_connect(self, result=0, error=None, tty=False):
        run = mock.Mock(return_value=result, side_effect=error)
        with mock.patch.object(cli.connect, "run", run), mock.patch.object(cli, "_context", return_value=self.ctx), \
                mock.patch("builtins.print") as printed, mock.patch("sys.stdin.isatty", return_value=tty), \
                mock.patch("builtins.input") as held:
            code = cli.cmd_connect(mock.Mock(label=None), self.ctx.paths)
        return code, " ".join(str(c.args[0]) for c in printed.call_args_list), held, run

    def test_connect_label_must_be_a_machine_name(self):
        run, api = mock.Mock(return_value=0), mock.Mock()
        with mock.patch.dict(os.environ, {"HOME": str(self.ctx.paths.home)}), \
                mock.patch.object(cli.connect, "run", run), mock.patch.object(cli, "MoshiApi", api), \
                mock.patch.object(cli, "default_store", side_effect=AssertionError("no store")), \
                mock.patch("sys.stdin.isatty", return_value=False), redirect_stderr(io.StringIO()) as err:
            self.assertEqual(cli.main(["connect", "--label", "a b"]), 1)
        self.assertIn("Not a machine name", err.getvalue())
        run.assert_not_called()
        api.assert_not_called()

    def test_the_label_and_the_headless_flag_reach_connect(self):
        paths = dataclasses.replace(self.ctx.paths, headless=True)
        run = mock.Mock(return_value=0)
        with mock.patch.object(cli.connect, "run", run), mock.patch.object(cli, "_context", return_value=self.ctx), \
                mock.patch("builtins.print"), mock.patch("sys.stdin.isatty", return_value=False):
            self.assertEqual(cli.cmd_connect(mock.Mock(label="build-1"), paths), 0)
        self.assertEqual((run.call_args.kwargs["label"], run.call_args.kwargs["headless"]), ("build-1", True))

    def test_it_talks_to_the_configured_moshi(self):
        code, _, _, run = self.run_connect()
        self.assertEqual(code, 0)
        api = run.call_args.args[2]
        self.assertIsInstance(api, MoshiApi)
        self.assertEqual(api._base, origin(MOSHI))

    def test_the_outcome_is_the_exit_code(self):
        self.assertEqual(self.run_connect(result=1)[0], 1)

    def test_the_outcome_stays_readable_until_enter(self):
        _, _, held, _ = self.run_connect(tty=True)
        held.assert_called_once()

    def test_a_failure_is_one_line_and_waits_too(self):
        for error in (ValueError("Name taken"), MoshiError("too_many_requests", 429), KeychainError("locked"),
                      OSError("network down")):
            code, text, held, _ = self.run_connect(error=error, tty=True)
            self.assertEqual(code, 1)
            self.assertIn(str(error), text)
            held.assert_called_once()

    def test_ctrl_c_is_one_line_and_still_waits(self):
        code, text, held, _ = self.run_connect(error=KeyboardInterrupt(), tty=True)
        self.assertEqual(code, 130)
        self.assertIn("Cancelled.", text)
        self.assertNotIn("Traceback", text)
        held.assert_called_once()

    def test_closed_input_is_one_line_and_does_not_wait(self):
        code, text, held, _ = self.run_connect(error=EOFError(), tty=True)
        self.assertEqual(code, 1)
        self.assertIn("Cancelled.", text)
        held.assert_not_called()

    def test_without_moshi_it_says_so(self):
        save_servers(self.ctx.paths, self.ctx.backup, {"moshi": ""})
        code, text, _, run = self.run_connect()
        self.assertEqual(code, 1)
        self.assertIn("moshi", text)
        run.assert_not_called()

    def test_without_a_terminal_nothing_waits(self):
        _, _, held, _ = self.run_connect()
        held.assert_not_called()

    def test_the_text_status_before_connect_names_the_way(self):
        args = mock.Mock(json=False)
        with mock.patch.object(cli, "default_store", return_value=self.ctx.keychain), \
                mock.patch("builtins.print") as printed:
            self.assertEqual(cli.cmd_status(args, self.ctx.paths), 0)
        self.assertIn("run moshi-cockpit connect", " ".join(str(c.args[0]) for c in printed.call_args_list))


if __name__ == "__main__":
    unittest.main()
