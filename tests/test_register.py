"""Registration tokens and the guided `agent add`."""

import io
import json
import threading
import unittest
import urllib.error
from contextlib import redirect_stdout
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, HTTPServer
from unittest import mock

from cockpit import admin, bots, cli, doctor, register, registry, wizard
from cockpit.agents import AgentContext
from cockpit.backup import BackupStore
from cockpit.secrets.macos import MacKeychain, KeychainError
from cockpit.tokens import REGISTER, bind_identity
from tests.helpers import MOSHI, FakeSecurity, bind_cockpit, configure, temp_paths

RT = "rt_01jc0000000000000000000000_" + "A" * 43
OTHER_RT = "rt_01jc0000000000000000000001_" + "B" * 43
NEW_TOKEN = "bt_NEWAGENT_0123456789"
ADMIN_TOKEN = "admin_TOKEN_0123456789"


class FakeRegisterApi(BaseHTTPRequestHandler):
    seen = []

    def do_POST(self):  # noqa: N802
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])) or b"{}")
        FakeRegisterApi.seen.append((self.path, self.headers.get("Authorization"), body))
        if self.path != "/api/register/agents":
            return self._send(404, {"error": "not_found"})
        if self.headers.get("Authorization") != f"Bearer {RT}":
            return self._send(401, {"error": "invalid_token"})
        name = body.get("name", "")
        if name.startswith("bob-"):
            return self._send(403, {"error": "name_prefix", "message": "Names must start with anna-\x1b[31m"})
        if name == "anna-taken":
            return self._send(409, {"error": "name_taken"})
        if name == "anna-busy":
            return self._send(429, {"error": "rate_limited"}, {"Retry-After": "120"})
        self._send(201, {"agent": {"id": "01X", "name": name}, "token": NEW_TOKEN})

    def _send(self, code, payload, headers=None):
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        for key, value in (headers or {}).items():
            self.send_header(key, value)
        self.end_headers()
        self.wfile.write(json.dumps(payload).encode())

    def log_message(self, *args):
        pass


class RegisterApiTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = HTTPServer(("127.0.0.1", 0), FakeRegisterApi)
        cls.base = f"http://127.0.0.1:{cls.server.server_port}/api/register"
        threading.Thread(target=cls.server.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()

    def api(self, token=RT, base=None):
        return register.RegisterApi(token, MOSHI, base=base or self.base)

    def test_create_and_errors_without_secrets(self):
        _, token = self.api().create_agent("anna-vibe")
        self.assertEqual(token, NEW_TOKEN)
        with self.assertRaises(admin.AdminError) as ctx:  # the reason travels as a field, not only as text
            self.api().create_agent("bob-vibe")
        self.assertEqual((ctx.exception.reason, ctx.exception.status), ("name_prefix", 403))
        cases = {"anna-taken": "schon vergeben", "anna-busy": "in 120 s", "bob-vibe": "Name nicht erlaubt"}
        for name, expected in cases.items():
            with self.assertRaises(admin.AdminError) as ctx:
                self.api().create_agent(name)
            self.assertIn(expected, str(ctx.exception))
            self.assertNotIn("\x1b", str(ctx.exception))
        with self.assertRaises(admin.AdminError) as ctx:
            self.api(OTHER_RT).create_agent("anna-vibe")
        self.assertIn("abgelehnt", str(ctx.exception))
        self.assertNotIn(OTHER_RT, str(ctx.exception))
        with self.assertRaises(admin.AdminError) as ctx:
            self.api(base=self.base.replace("/api/register", "/nothing")).create_agent("anna-vibe")
        self.assertIn("nicht deployt", str(ctx.exception))

    def ctx(self, entries):
        paths = temp_paths(self)
        configure(paths, agents=[{"name": "vibe", "harness": "vibe"}])
        fake = FakeSecurity(entries)
        return AgentContext(paths=paths, backup=BackupStore(paths.state), keychain=MacKeychain(fake)), fake

    def test_bot_add_prefers_registration_token_over_admin(self):
        ctx, fake = self.ctx({(register.REGISTER_SERVICE, register.REGISTER_ACCOUNT): RT,
                              (admin.ADMIN_SERVICE, admin.ADMIN_ACCOUNT): ADMIN_TOKEN,
                              ("ocellio-vibe", "vibe"): "pt_OCELLIO_0123456789"})
        io = bots.BotIO(ask_secret=lambda _p: self.fail("no prompt"), open_url=lambda _u: self.fail("no browser"),
                        say=lambda _m: None, register_api=lambda: self.api(),
                        admin_api=lambda: self.fail("admin token must not be used"))
        notes = bots.add("vibe", ctx, io)
        self.assertIn("per Registrier-Token", notes[0])
        self.assertEqual(fake.entries[("moshi-vibe", "vibe")], NEW_TOKEN)
        self.assertEqual(FakeRegisterApi.seen[-1][2], {"name": "vibe"})
        self.assertFalse(fake.argv_contains(NEW_TOKEN) or fake.argv_contains(RT))

    def test_refused_registration_token_never_falls_back_to_admin(self):
        ctx, fake = self.ctx({(register.REGISTER_SERVICE, register.REGISTER_ACCOUNT): OTHER_RT,
                              (admin.ADMIN_SERVICE, admin.ADMIN_ACCOUNT): ADMIN_TOKEN})
        io = bots.BotIO(ask_secret=lambda _p: self.fail("no prompt"), open_url=lambda _u: self.fail("no browser"),
                        say=lambda _m: None, register_api=lambda: self.api(OTHER_RT),
                        admin_api=lambda: self.fail("admin token must not be used"))
        with self.assertRaises(admin.AdminError):
            bots.add("vibe", ctx, io)
        self.assertNotIn(("moshi-vibe", "vibe"), fake.entries)

    def test_browser_flag_skips_registration_token(self):
        ctx, fake = self.ctx({(register.REGISTER_SERVICE, register.REGISTER_ACCOUNT): RT,
                              ("ocellio-vibe", "vibe"): "pt_OCELLIO_0123456789"})
        opened = []
        io = bots.BotIO(ask_secret=lambda _p: NEW_TOKEN, open_url=lambda u: opened.append(u) or True,
                        say=lambda _m: None, register_api=lambda: self.fail("API must not be used"))
        bots.add("vibe", ctx, io, browser=True)
        self.assertEqual(opened, ["https://moshi.example.test/agents/connect"])

    def test_fallback_matrix_admin_with_notice_then_connect_page(self):
        # (c) no registration token, admin token there: admin, and it says so before reading it
        ctx, fake = self.ctx({(admin.ADMIN_SERVICE, admin.ADMIN_ACCOUNT): ADMIN_TOKEN,
                              ("ocellio-vibe", "vibe"): "pt_OCELLIO_0123456789"})
        said, used = [], []

        class Api:
            def create_agent(self, name):
                used.append(name)
                return {"id": "01X"}, NEW_TOKEN
        io = bots.BotIO(ask_secret=lambda _p: self.fail("no prompt"), open_url=lambda _u: self.fail("no browser"),
                        say=said.append, admin_api=Api)
        notes = bots.add("vibe", ctx, io)
        self.assertTrue(any("Admin-Token" in line for line in said))
        self.assertIn("per Admin-Token", notes[0])
        self.assertEqual(used, ["vibe"])
        # The agent token sits in the agent's slot, bound like every agent; the token slots stay apart
        self.assertEqual(fake.entries[("moshi-vibe", "vibe")], NEW_TOKEN)
        self.assertEqual(registry.find(ctx.paths, "vibe").bound["moshi"], "https://moshi.example.test")
        self.assertNotIn((register.REGISTER_SERVICE, register.REGISTER_ACCOUNT), fake.entries)
        # (d) neither token: the connect page
        ctx, fake = self.ctx({("ocellio-vibe", "vibe"): "pt_OCELLIO_0123456789"})
        opened = []
        io = bots.BotIO(ask_secret=lambda _p: NEW_TOKEN, open_url=lambda u: opened.append(u) or True,
                        say=lambda _m: None, admin_api=lambda: self.fail("no admin token"),
                        register_api=lambda: self.fail("no registration token"))
        bots.add("vibe", ctx, io)
        self.assertEqual(opened, ["https://moshi.example.test/agents/connect"])

    def test_refusal_exits_nonzero_through_the_cli(self):
        ctx, fake = self.ctx({(register.REGISTER_SERVICE, register.REGISTER_ACCOUNT): OTHER_RT,
                              (admin.ADMIN_SERVICE, admin.ADMIN_ACCOUNT): ADMIN_TOKEN})
        bind_cockpit(ctx.paths, identity="register")
        api = self.api(OTHER_RT)
        with mock.patch("cockpit.cli.Paths.from_env", return_value=ctx.paths), \
                mock.patch("cockpit.cli.default_store", return_value=ctx.keychain), \
                mock.patch("cockpit.bots.RegisterApi.from_keychain", return_value=api), \
                mock.patch("cockpit.bots.AdminApi.from_keychain", side_effect=AssertionError("admin used")):
            self.assertEqual(cli.main(["bot", "add", "vibe"]), 1)
        self.assertNotIn(("moshi-vibe", "vibe"), fake.entries)


class AutoConnectTest(unittest.TestCase):  # "New agent" is one step: created and connected, no questions
    OCELLIO = "pt_OCELLIO_0123456789"

    def ctx(self, entries, sibling_bound=True):
        paths = temp_paths(self)
        configure(paths, agents=[{"name": "neu", "harness": "vibe", "bound": {}},
                                                {"name": "alt", "harness": "codex",
                                                 "bound": {"ocellio": "https://ocellio.example.test"} if sibling_bound else {}}])
        fake = FakeSecurity(entries)
        return AgentContext(paths=paths, backup=BackupStore(paths.state), keychain=MacKeychain(fake)), fake

    def api(self):
        class Api:
            created = []
            def create_agent(self, name):
                Api.created.append(name)
                return {"id": "01X"}, NEW_TOKEN
        return Api

    def io(self, api):
        return bots.BotIO(ask_secret=lambda _p: self.fail("auto must not ask"), open_url=lambda _u: self.fail("auto must not open a browser"),
                          say=lambda _m: None, admin_api=api)

    def test_takes_the_persons_ocellio_token_and_creates_moshi_without_a_question(self):
        ctx, fake = self.ctx({(admin.ADMIN_SERVICE, admin.ADMIN_ACCOUNT): ADMIN_TOKEN, ("ocellio-alt", "alt"): self.OCELLIO})
        api = self.api()
        notes = bots.add("neu", ctx, self.io(api), auto=True)
        self.assertEqual(api.created, ["neu"])
        self.assertEqual(fake.entries[("ocellio-neu", "neu")], self.OCELLIO)
        self.assertEqual(fake.entries[("moshi-neu", "neu")], NEW_TOKEN)
        self.assertNotIn(self.OCELLIO, " ".join(notes))
        self.assertFalse(fake.argv_contains(self.OCELLIO))

    def test_a_copy_is_at_least_as_protected_as_its_source(self):
        for hardened in (True, False):
            with self.subTest(hardened=hardened):
                ctx, fake = self.ctx({(admin.ADMIN_SERVICE, admin.ADMIN_ACCOUNT): ADMIN_TOKEN, ("ocellio-alt", "alt"): self.OCELLIO})
                fake.strict[("ocellio-alt", "alt")] = hardened
                bots.add("neu", ctx, self.io(self.api()), auto=True)
                self.assertEqual(MacKeychain(fake).is_strict("ocellio-neu", "neu"), hardened)

    def test_stops_before_creating_anything_when_a_token_cannot_be_found(self):
        cases = [({(admin.ADMIN_SERVICE, admin.ADMIN_ACCOUNT): ADMIN_TOKEN}, True),  # no ocellio token here
                 ({(admin.ADMIN_SERVICE, admin.ADMIN_ACCOUNT): ADMIN_TOKEN, ("ocellio-alt", "alt"): self.OCELLIO}, False),  # other server
                 ({("ocellio-alt", "alt"): self.OCELLIO}, True)]  # no way to create the moshi identity
        for entries, bound in cases:
            with self.subTest(entries=len(entries), bound=bound):
                ctx, fake = self.ctx(entries, sibling_bound=bound)
                api = self.api()
                with self.assertRaises(ValueError):
                    bots.add("neu", ctx, self.io(api), auto=True)
                self.assertEqual(api.created, [])
                self.assertNotIn(("moshi-neu", "neu"), fake.entries)

    def test_add_with_connect_keeps_the_agent_and_says_what_is_missing(self):
        paths = temp_paths(self)
        configure(paths)
        fake = FakeSecurity()
        with mock.patch("cockpit.cli.Paths.from_env", return_value=paths), \
                mock.patch("cockpit.cli.default_store", return_value=MacKeychain(fake)), redirect_stdout(io.StringIO()) as out:
            self.assertEqual(cli.main(["agent", "add", "neu", "--harness=codex", "--connect"]), 0)
        self.assertIn("! Noch nicht angemeldet", out.getvalue())
        self.assertEqual(registry.find(paths, "neu").harness, "codex")

    def test_connect_survives_a_network_error(self):  # the agent exists; only the connection is missing
        paths = temp_paths(self)
        configure(paths)
        with mock.patch("cockpit.cli.Paths.from_env", return_value=paths), \
                mock.patch("cockpit.cli.default_store", return_value=MacKeychain(FakeSecurity())), \
                mock.patch("cockpit.cli.bots.add", side_effect=urllib.error.URLError("offline")), \
                redirect_stdout(io.StringIO()) as out:
            self.assertEqual(cli.main(["agent", "add", "neu", "--harness=codex", "--connect"]), 0)
        self.assertIn("! Noch nicht angemeldet", out.getvalue())
        self.assertEqual(registry.find(paths, "neu").harness, "codex")

    def test_a_parked_ocellio_entry_is_restored_not_replaced_by_a_copy(self):
        ctx, _ = self.ctx({})
        api = self.api()
        with mock.patch.object(MacKeychain, "restore_parked", return_value=True), \
                mock.patch("cockpit.bots._person_ocellio_token", side_effect=AssertionError("no copy")), \
                mock.patch("cockpit.bots._store_moshi", side_effect=ValueError("stop here")), \
                self.assertRaises(ValueError):
            bots.add("neu", ctx, self.io(api), auto=True)


class RegistrationTokenStorageTest(unittest.TestCase):
    def test_stored_with_dialog_and_only_in_its_format(self):
        fake = FakeSecurity()
        register.store_register_token(MacKeychain(fake), RT)
        self.assertTrue(MacKeychain(fake).is_strict(register.REGISTER_SERVICE, register.REGISTER_ACCOUNT))
        self.assertFalse(fake.argv_contains(RT))
        for wrong in (ADMIN_TOKEN, NEW_TOKEN, RT + "x", "rt_short"):
            with self.assertRaises(KeychainError) as ctx:
                register.store_register_token(MacKeychain(fake), wrong)
            self.assertNotIn(wrong, str(ctx.exception))

    def test_token_only_goes_to_the_origin_it_was_bound_to(self):
        paths = temp_paths(self)
        configure(paths)
        keychain = MacKeychain(FakeSecurity({(register.REGISTER_SERVICE, register.REGISTER_ACCOUNT): RT}))
        with self.assertRaises(ValueError) as ctx:  # not bound yet
            register.RegisterApi.from_keychain(keychain, MOSHI, paths)
        self.assertIn("register-token set", str(ctx.exception))
        bind_identity(paths, BackupStore(paths.state), REGISTER, MOSHI)
        self.assertIsNotNone(register.RegisterApi.from_keychain(keychain, MOSHI, paths))
        with self.assertRaises(ValueError):
            register.RegisterApi.from_keychain(keychain, "https://elsewhere.example.test/mcp", paths)
        with self.assertRaises(ValueError):  # a move needs --rebind
            bind_identity(paths, BackupStore(paths.state), REGISTER, "https://elsewhere.example.test/mcp")

    def test_cli_set_binds_and_stores_without_argv_or_output(self):
        paths = temp_paths(self)
        configure(paths)
        fake = FakeSecurity()
        with mock.patch("cockpit.cli.Paths.from_env", return_value=paths), \
                mock.patch("cockpit.cli.default_store", return_value=MacKeychain(fake)), \
                mock.patch("cockpit.cli.bots.ask_token", return_value=RT), \
                mock.patch("sys.stdout") as out:
            self.assertEqual(cli.main(["bot", "register-token", "set"]), 0)
        printed = "".join(str(c) for c in out.mock_calls)
        self.assertNotIn(RT, printed)
        self.assertFalse(fake.argv_contains(RT))
        self.assertTrue(fake.strict[(register.REGISTER_SERVICE, register.REGISTER_ACCOUNT)])
        self.assertEqual(json.loads(paths.mesh_config.read_text())["bound"]["register"], "https://moshi.example.test")

    def test_cli_set_from_clipboard_checks_format_and_clears_it(self):
        paths = temp_paths(self)
        configure(paths)
        fake, clipboard = FakeSecurity(), {"text": "moshi-cockpit bot register-token set"}

        def runner(args, input=None, **_kwargs):
            if args == ["pbpaste"]:
                return mock.Mock(returncode=0, stdout=clipboard["text"])
            clipboard["text"] = input
            return mock.Mock(returncode=0, stdout="")
        with mock.patch("cockpit.cli.Paths.from_env", return_value=paths), \
                mock.patch("cockpit.cli.default_store", return_value=MacKeychain(fake)), \
                mock.patch("cockpit.register.subprocess.run", side_effect=runner), \
                mock.patch("cockpit.cli.bots.ask_token", side_effect=AssertionError("no prompt")), \
                mock.patch("sys.stdout"), mock.patch("sys.stderr") as err:
            self.assertEqual(cli.main(["bot", "register-token", "set", "--clipboard"]), 1)  # something else in it
            self.assertNotIn("register-token set", "".join(str(c) for c in err.mock_calls))
            self.assertEqual(clipboard["text"], "moshi-cockpit bot register-token set")  # left alone
            clipboard["text"] = RT + "\n"
            self.assertEqual(cli.main(["bot", "register-token", "set", "--clipboard"]), 0)
        self.assertEqual(fake.entries[(register.REGISTER_SERVICE, register.REGISTER_ACCOUNT)], RT)
        self.assertEqual(clipboard["text"], "")
        self.assertFalse(fake.argv_contains(RT))

    def test_clipboard_is_left_alone_when_something_else_was_copied(self):
        clipboard = {"text": "anderes"}

        def runner(args, input=None, **_kwargs):
            if args == ["pbpaste"]:
                return mock.Mock(returncode=0, stdout=clipboard["text"])
            clipboard["text"] = input
            return mock.Mock(returncode=0, stdout="")
        self.assertFalse(register.clear_clipboard(RT, runner))
        self.assertEqual(clipboard["text"], "anderes")
        clipboard["text"] = RT
        self.assertTrue(register.clear_clipboard(RT, runner))
        self.assertEqual(clipboard["text"], "")

    def test_failed_store_says_the_token_is_still_in_the_clipboard(self):
        paths = temp_paths(self)
        configure(paths)
        bind_cockpit(paths, url="https://elsewhere.example.test/mcp", identity="register")  # moved: needs --rebind
        fake = FakeSecurity()

        def runner(args, input=None, **_kwargs):
            return mock.Mock(returncode=0, stdout=RT)
        with mock.patch("cockpit.cli.Paths.from_env", return_value=paths), \
                mock.patch("cockpit.cli.default_store", return_value=MacKeychain(fake)), \
                mock.patch("cockpit.register.subprocess.run", side_effect=runner), \
                mock.patch("sys.stdout"), mock.patch("sys.stderr") as err:
            self.assertEqual(cli.main(["bot", "register-token", "set", "--clipboard"]), 1)
        message = "".join(str(c) for c in err.mock_calls)
        self.assertIn("liegt noch in der Zwischenablage", message)
        self.assertNotIn(RT, message)
        self.assertEqual(fake.entries, {})

    def test_doctor_checks_binding_without_reading_the_token(self):
        paths = temp_paths(self)
        configure(paths)
        fake = FakeSecurity({(register.REGISTER_SERVICE, register.REGISTER_ACCOUNT): RT})
        ctx = doctor.DoctorContext(paths=paths, keychain=MacKeychain(fake), pins={})
        [unbound] = [c for c in doctor.check_identities(ctx) if c.name == "identity-register"]
        self.assertEqual(unbound.status, doctor.FAIL)
        bind_cockpit(paths, url="https://elsewhere.example.test/mcp", identity="register")
        [moved] = [c for c in doctor.check_identities(ctx) if c.name == "identity-register"]
        self.assertEqual(moved.status, doctor.FAIL)
        bind_cockpit(paths, identity="register")
        [ok] = [c for c in doctor.check_identities(ctx) if c.name == "identity-register"]
        self.assertEqual(ok.status, doctor.OK)
        self.assertFalse(any("-w" in args for args, _ in fake.calls))
        self.assertNotIn(RT, unbound.detail + moved.detail + ok.detail)

    def test_remove_drops_entry_and_binding(self):
        paths = temp_paths(self)
        configure(paths)
        fake = FakeSecurity({(register.REGISTER_SERVICE, register.REGISTER_ACCOUNT): RT})
        bind_cockpit(paths, identity="register")
        with mock.patch("cockpit.cli.Paths.from_env", return_value=paths), \
                mock.patch("cockpit.cli.default_store", return_value=MacKeychain(fake)), mock.patch("sys.stdout"):
            self.assertEqual(cli.main(["bot", "register-token", "remove"]), 0)
            self.assertEqual(cli.main(["bot", "register-token", "remove"]), 0)  # twice is fine
        self.assertNotIn((register.REGISTER_SERVICE, register.REGISTER_ACCOUNT), fake.entries)
        self.assertNotIn("register", json.loads(paths.mesh_config.read_text()).get("bound", {}))
        self.assertFalse(any("-w" in args for args, _ in fake.calls))

    def test_role_loses_control_characters_like_the_panel(self):
        agent = registry._validate({"name": "a", "harness": "codex", "role": " Recherche \x1b[31m und\x00\tSuche\n "})
        self.assertEqual(agent.role, "Recherche [31m und Suche")  # same as rules.js roleText

    def test_register_is_a_reserved_agent_name(self):
        with self.assertRaises(ValueError):
            registry.validate_name("register")


class FakeAdmin:
    def __init__(self):
        self.created, self.revoked = [], []

    def create_registration_token(self, label, name_prefix, expires_at, max_per_hour=None):
        self.created.append((label, name_prefix, expires_at, max_per_hour))
        return {"id": "01JC0000000000000000000000", "name_prefix": name_prefix, "expires_at": expires_at}, RT

    def revoke_registration_token(self, token_id):
        self.revoked.append(token_id)
        return {"id": token_id}


class RevokeTest(unittest.TestCase):
    def test_only_a_ulid_reaches_the_url_path(self):
        seen = []
        api = admin.AdminApi(ADMIN_TOKEN, MOSHI, opener=lambda req, timeout: seen.append(req.full_url))
        for bad in ("../agents", "01JC0000000000000000000000/x", "", "01jc0000000000000000000000"):
            with self.assertRaises(admin.AdminError):
                api.revoke_registration_token(bad)
        self.assertEqual(seen, [])


class IssueTest(unittest.TestCase):
    def test_always_with_prefix_and_expiry_into_the_clipboard(self):
        api, copied = FakeAdmin(), []
        created = register.issue(api, "Anna", days=3, copy=copied.append)
        label, prefix, expires, _ = api.created[0]
        self.assertEqual((label, prefix), ("moshi-cockpit anna", "anna-"))
        delta = datetime.fromisoformat(expires.replace("Z", "+00:00")) - datetime.now(timezone.utc)
        self.assertTrue(2.9 < delta.total_seconds() / 86400 <= 3)
        self.assertEqual(copied, [RT])
        self.assertNotIn(RT, json.dumps(created))

    def test_clipboard_failure_revokes_at_once(self):
        api = FakeAdmin()

        def broken(_secret):
            raise KeychainError("Zwischenablage nicht beschreibbar (pbcopy)")
        with self.assertRaises(KeychainError) as ctx:
            register.issue(api, "anna", copy=broken)
        self.assertEqual(api.revoked, ["01JC0000000000000000000000"])
        self.assertNotIn(RT, str(ctx.exception))

    def test_failed_revoke_is_reported_honestly(self):
        def broken(_secret):
            raise KeychainError("Zwischenablage nicht beschreibbar (pbcopy)")

        class RevokeFails(FakeAdmin):
            def revoke_registration_token(self, token_id):
                raise admin.AdminError("moshi nicht erreichbar")
        with self.assertRaises(KeychainError) as ctx:
            register.issue(RevokeFails(), "anna", copy=broken)
        self.assertIn("NICHT widerrufen", str(ctx.exception))
        self.assertIn("revoke 01JC0000000000000000000000", str(ctx.exception))

        class NoId(FakeAdmin):
            def create_registration_token(self, *args, **kwargs):
                return {}, RT
        with self.assertRaises(KeychainError) as ctx:  # no id: nothing to revoke, and it says so
            register.issue(NoId(), "anna", copy=broken)
        self.assertIn("NICHT widerrufen", str(ctx.exception))
        self.assertIn("register-token list", str(ctx.exception))
        self.assertNotIn(RT, str(ctx.exception))

    def test_limits_are_checked_before_anything_is_issued(self):
        api = FakeAdmin()
        for kwargs in ({"days": 0}, {"days": register.MAX_DAYS + 1}):
            with self.assertRaises(ValueError):
                register.issue(api, "anna", copy=lambda _s: None, **kwargs)
        with self.assertRaises(ValueError):
            register.issue(api, "a" * 41, copy=lambda _s: None)  # prefix would be 42 characters
        with self.assertRaises(ValueError):
            register.issue(api, "../x", copy=lambda _s: None)
        self.assertEqual(api.created, [])

    def test_clipboard_gets_the_secret_through_stdin(self):
        calls = []

        def runner(args, input="", **_kwargs):
            calls.append((args, input))
            return mock.Mock(returncode=0)
        register.copy_to_clipboard(RT, runner)
        self.assertEqual(calls, [(["pbcopy"], RT)])


class WizardTest(unittest.TestCase):
    def setUp(self):
        self.paths = temp_paths(self)
        self.paths.local_bin.mkdir(parents=True, exist_ok=True)
        for binary in ("vibe", "codex", "agy", "claude", "grok"):
            (self.paths.local_bin / binary).write_text("")
        patcher = mock.patch("cockpit.harnesses.base.shutil.which", return_value=None)
        patcher.start()
        self.addCleanup(patcher.stop)

    def ctx(self, servers=None, agents=()):
        configure(self.paths, servers=servers, agents=agents)
        return AgentContext(paths=self.paths, backup=BackupStore(self.paths.state), keychain=MacKeychain(FakeSecurity()))

    def script(self, *answers):
        queue, said = list(answers), []
        return wizard.WizardIO(ask=lambda _p: queue.pop(0), say=said.append), said

    def test_asks_name_harness_role_then_connects(self):
        ctx = self.ctx()
        io, said = self.script("register", "recherche", "9", "codex", "Recherche", "")
        connected = []
        notes = wizard.run(ctx, io, connect=lambda name: connected.append(name) or [f"{name} angebunden"])
        agent = registry.find(self.paths, "recherche")
        self.assertEqual((agent.harness, agent.role), ("codex", "Recherche"))
        self.assertEqual(connected, ["recherche"])
        self.assertIn("recherche angebunden", notes)
        self.assertTrue(any("reserviert" in line for line in said))

    def test_without_servers_only_the_local_agent(self):
        ctx = self.ctx(servers={})
        io, _ = self.script("lokal", "vibe", "")
        notes = wizard.run(ctx, io, connect=lambda _n: self.fail("no servers, nothing to connect"))
        self.assertEqual(registry.find(self.paths, "lokal").harness, "vibe")
        self.assertIn("moshi-cockpit run lokal", notes[0])

    def test_offers_only_installed_and_free_harnesses_and_says_why(self):
        ctx = self.ctx(agents=[{"name": "agy", "harness": "agy"}])
        (self.paths.local_bin / "grok").unlink()
        self.assertEqual(wizard.harness_choices(ctx.paths), ["claude", "codex", "vibe"])
        reasons = wizard.unavailable(ctx.paths)
        offer = {h["name"]: h for h in wizard.harness_offer(ctx.paths)}
        self.assertFalse(offer["agy"]["available"])
        self.assertIn("'agy'", offer["agy"]["reason"])
        self.assertTrue(offer["codex"]["available"] and offer["codex"]["native_mesh"])
        self.assertTrue(offer["claude"]["native_mesh"])
        self.assertTrue(any(r.startswith("agy:") and "'agy'" in r for r in reasons))
        self.assertTrue(any(r.startswith("grok: verbindet sich über dein Konto") for r in reasons))
        self.assertFalse(offer["grok"]["available"])
        self.assertIn("„+“ → Grok", offer["grok"]["reason"])

    def test_grok_cannot_be_created_as_an_own_agent(self):
        ctx = self.ctx()
        with self.assertRaises(ValueError) as caught:
            wizard.create(ctx, "g", "grok")
        self.assertIn("eigene moshi-Identität", str(caught.exception))
        self.assertEqual([a.name for a in registry.load_agents(ctx.paths)], [])

    def test_skill_is_stored_checked_and_changeable(self):
        ctx = self.ctx()
        wizard.create(ctx, "rev", "codex", "Reviewer", "skill-a")
        self.assertEqual(registry.find(ctx.paths, "rev").skill, "skill-a")
        changed = registry.update(ctx.paths, ctx.backup, "rev", None, "skill-b")
        self.assertEqual((changed.role, changed.skill), ("Reviewer", "skill-b"))
        self.assertEqual(registry.update(ctx.paths, ctx.backup, "rev", "", "").role, "")
        self.assertEqual(registry.find(ctx.paths, "rev").skill, "")
        for bad in ("../x", "a b", "-x", "a`b"):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                registry.update(ctx.paths, ctx.backup, "rev", None, bad)
        with self.assertRaises(ValueError):
            registry.update(ctx.paths, ctx.backup, "nobody", "x", None)

    def test_skill_without_ocellio_says_it_is_ignored(self):
        ctx = self.ctx()
        configure(ctx.paths, servers={"moshi": "https://moshi.example.test/mcp"})
        self.assertTrue(any("ignoriert" in n for n in wizard.create(ctx, "rev", "codex", "", "skill-a")))
        self.assertFalse(any("ignoriert" in n for n in wizard.create(ctx, "rev2", "codex")))

    def test_declined_connect_leaves_a_hint(self):
        ctx = self.ctx()
        io, _ = self.script("n")
        notes = wizard.run(ctx, io, name="spaeter", harness="vibe", role="",
                           connect=lambda _n: self.fail("declined"))
        self.assertIn("moshi-cockpit bot add spaeter", notes[0])

    def test_cli_without_terminal_needs_name_and_harness(self):
        configure(self.paths)
        with mock.patch("cockpit.cli.Paths.from_env", return_value=self.paths), \
                mock.patch("sys.stdin.isatty", return_value=False):
            self.assertEqual(cli.main(["agent", "add"]), 1)
            self.assertEqual(cli.main(["agent", "add", "flags", "--harness", "vibe"]), 0)
        self.assertEqual(registry.find(self.paths, "flags").harness, "vibe")


if __name__ == "__main__":
    unittest.main()
