import json
import threading
import unittest
from http.server import BaseHTTPRequestHandler, HTTPServer

from cockpit import admin, bots
from cockpit.agents import AgentContext
from cockpit.backup import BackupStore
from cockpit.secrets.macos import MacKeychain
from tests.helpers import FakeSecurity, configure, temp_paths

ADMIN_TOKEN = "admin_TOKEN_0123456789"
NEW_TOKEN = "bt_NEWAGENT_0123456789"


class FakeAdminApi(BaseHTTPRequestHandler):
    seen = []

    def _auth_ok(self):
        return self.headers.get("Authorization") == f"Bearer {ADMIN_TOKEN}"

    def do_POST(self):  # noqa: N802
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])) or b"{}")
        FakeAdminApi.seen.append((self.path, body))
        if not self._auth_ok():
            return self._send(403, {"error": "forbidden"})
        if body.get("name") == "taken":
            return self._send(400, {"error": "name \x1b[31mtaken"})
        self._send(201, {"agent": {"id": "01X", "name": body["name"]}, "token": NEW_TOKEN})

    def do_GET(self):  # noqa: N802
        if not self._auth_ok():
            return self._send(403, {"error": "forbidden"})
        self._send(404, {})

    def _send(self, code, payload):
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(json.dumps(payload).encode())

    def log_message(self, *args):
        pass


class AdminTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = HTTPServer(("127.0.0.1", 0), FakeAdminApi)
        cls.base = f"http://127.0.0.1:{cls.server.server_port}/api/admin"
        threading.Thread(target=cls.server.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()

    def api(self, token=ADMIN_TOKEN):
        return admin.AdminApi(token, "https://moshi.example.test/mcp", base=self.base)

    def test_create_returns_token_and_errors_never_leak_it(self):
        _, token = self.api().create_agent("vibe")
        self.assertEqual(token, NEW_TOKEN)
        with self.assertRaises(admin.AdminError) as ctx:
            self.api().create_agent("taken")
        self.assertNotIn("\x1b", str(ctx.exception))
        with self.assertRaises(admin.AdminError) as ctx:
            self.api("wrong_token_123456").create_agent("vibe")
        self.assertNotIn("wrong_token", str(ctx.exception))

    def test_unknown_endpoint_explains_missing_deploy(self):
        with self.assertRaises(admin.AdminError) as ctx:
            admin.AdminApi(ADMIN_TOKEN, "https://moshi.example.test/mcp", base=self.base.replace("/api/admin", "/nothing")).registration_tokens()
        self.assertIn("nicht deployt", str(ctx.exception))

    def test_admin_token_is_always_stored_with_dialog(self):
        fake = FakeSecurity()
        admin.store_admin_token(MacKeychain(fake), ADMIN_TOKEN)
        self.assertTrue(any('-T ""' in stdin for _args, stdin in fake.calls))
        self.assertTrue(MacKeychain(fake).is_strict(admin.ADMIN_SERVICE, admin.ADMIN_ACCOUNT))
        self.assertFalse(fake.argv_contains(ADMIN_TOKEN))

    def test_bot_add_uses_api_when_admin_token_present(self):
        paths = temp_paths(self)
        fake = FakeSecurity({(admin.ADMIN_SERVICE, admin.ADMIN_ACCOUNT): ADMIN_TOKEN})
        configure(paths)
        ctx = AgentContext(paths=paths, backup=BackupStore(paths.state), keychain=MacKeychain(fake))
        opened = []
        io = bots.BotIO(ask_secret=lambda _p: "unused", open_url=lambda u: opened.append(u) or True,
                        say=lambda _m: None, admin_api=lambda: self.api())
        notes = bots.add("cockpit", ctx, io)
        self.assertIn("per Admin-Token", notes[0])
        self.assertEqual(fake.entries[("moshi-cockpit", "cockpit")], NEW_TOKEN)
        self.assertEqual(opened, [])
        self.assertFalse(fake.argv_contains(NEW_TOKEN))

    def test_bot_add_browser_flag_skips_api(self):
        paths = temp_paths(self)
        fake = FakeSecurity({(admin.ADMIN_SERVICE, admin.ADMIN_ACCOUNT): ADMIN_TOKEN})
        configure(paths)
        ctx = AgentContext(paths=paths, backup=BackupStore(paths.state), keychain=MacKeychain(fake))
        opened = []
        io = bots.BotIO(ask_secret=lambda _p: NEW_TOKEN, open_url=lambda u: opened.append(u) or True,
                        say=lambda _m: None, admin_api=lambda: self.fail("API must not be used"))
        bots.add("cockpit", ctx, io, browser=True)
        self.assertEqual(opened, ["https://moshi.example.test/agents/connect"])

    def test_bot_add_cockpit_needs_moshi(self):
        paths = temp_paths(self)
        configure(paths, servers={})
        ctx = AgentContext(paths=paths, backup=BackupStore(paths.state), keychain=MacKeychain(FakeSecurity()))
        io = bots.BotIO(ask_secret=lambda _p: self.fail("no prompt"), open_url=lambda _u: True, say=lambda _m: None)
        with self.assertRaises(ValueError):
            bots.add("cockpit", ctx, io)


if __name__ == "__main__":
    unittest.main()
