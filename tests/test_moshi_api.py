"""MoshiApi against FakeMoshi: one test per call, error mapping, and that tokens never leak."""

import json
import socket
import threading
import unittest
from http.server import BaseHTTPRequestHandler, HTTPServer

from cockpit.mcp import McpClient, McpError
from cockpit.moshi_api import DeviceGrant, DeviceStart, MoshiApi, MoshiError
from tests.fake_moshi import serve


class MoshiApiTest(unittest.TestCase):
    def setUp(self):
        self.moshi = serve(self)
        self.api = MoshiApi(self.moshi.url)  # the MCP URL as config stores it
        self.installation, self.device_key = self.moshi.add_installation("Work Mac")
        self.rt = self.moshi.add_registration_token(self.installation["id"])

    def error_of(self, call, *args):
        with self.assertRaises(MoshiError) as ctx:
            call(*args)
        return ctx.exception

    def test_device_authorize_starts_a_flow(self):
        start = self.api.device_authorize("Work Mac")
        self.assertIsInstance(start, DeviceStart)
        self.assertTrue(start.device_code and start.user_code)
        self.assertEqual(start.verification_uri, self.moshi.origin + "/device")
        self.assertIn(start.user_code, start.verification_uri_complete)
        self.assertEqual((start.expires_in, start.interval), (900, 5))
        call = self.moshi.calls("POST", "/api/device/authorize")[0]
        self.assertEqual(call["body"], {"label": "Work Mac"})
        self.assertIsNone(call["auth"])

    def test_device_authorize_renewal_sends_the_device_key(self):
        self.api.device_authorize("Work Mac", device_key=self.device_key)
        self.assertEqual(self.moshi.calls("POST", "/api/device/authorize")[0]["auth"], f"Bearer {self.device_key}")

    def test_device_authorize_errors_pass_through(self):
        self.assertEqual(self.error_of(self.api.device_authorize, "Mac", "dk_revoked").reason, "invalid_device_key")
        self.moshi.too_many_requests = True
        error = self.error_of(self.api.device_authorize, "Mac")
        self.assertEqual((error.reason, error.status), ("too_many_requests", 429))

    def test_device_token_reports_each_pending_state_as_a_string(self):
        start = self.api.device_authorize("Work Mac")
        self.assertEqual(self.api.device_token(start.device_code), "authorization_pending")
        self.moshi.slow_down = 1
        self.assertEqual(self.api.device_token(start.device_code), "slow_down")
        self.moshi.deny(start.user_code)
        self.assertEqual(self.api.device_token(start.device_code), "access_denied")
        self.moshi.expire(start.user_code)
        self.assertEqual(self.api.device_token(start.device_code), "expired_token")

    def test_device_token_returns_the_grant(self):
        start = self.api.device_authorize("Home Mac")
        self.moshi.approve(start.user_code)
        grant = self.api.device_token(start.device_code)
        self.assertIsInstance(grant, DeviceGrant)
        self.assertTrue(grant.device_key.startswith("dk_"))
        self.assertTrue(grant.registration_token.startswith("rt_"))
        self.assertEqual(grant.installation_label, "Home Mac")
        self.assertEqual(self.moshi.installations[grant.installation_id]["label"], "Home Mac")
        self.assertRegex(grant.registration_expires_at, r"^\d{4}-\d\d-\d\dT")

    def test_device_token_on_renewal_has_no_new_device_key(self):
        start = self.api.device_authorize("Work Mac", device_key=self.device_key)
        self.moshi.approve(start.user_code)
        grant = self.api.device_token(start.device_code)
        self.assertIsNone(grant.device_key)
        self.assertEqual(grant.installation_id, self.installation["id"])

    def test_installation_status_with_the_device_key(self):
        self.moshi.add_agent("vibe", role="reviewer", presence="live")
        status = self.api.installation_status(self.device_key)
        self.assertEqual(status["installation"], {"id": self.installation["id"], "label": "Work Mac"})
        self.assertEqual(status["agents"][0]["name"], "vibe")
        self.assertEqual(status["agents"][0]["presence"], "live")
        self.assertEqual(self.moshi.calls("GET", "/api/installation/status")[0]["auth"], f"Bearer {self.device_key}")

    def test_installation_status_with_a_revoked_device_key(self):
        self.moshi.revoke_installation(self.installation["id"])
        error = self.error_of(self.api.installation_status, self.device_key)
        self.assertEqual((error.reason, error.status), ("invalid_device_key", 401))

    def test_register_agent_returns_profile_and_token(self):
        agent, token = self.api.register_agent(self.rt, "scout", "codex", "reviewer", "triage")
        self.assertEqual((agent["name"], agent["harness"], agent["role"], agent["skill"]),
                         ("scout", "codex", "reviewer", "triage"))
        self.assertTrue(token.startswith("bt_"))
        call = self.moshi.calls("POST", "/api/register/agents")[0]
        self.assertEqual(call["auth"], f"Bearer {self.rt}")
        self.assertEqual(call["body"], {"name": "scout", "harness": "codex", "role": "reviewer", "skill": "triage"})

    def test_register_agent_leaves_out_missing_role_and_skill(self):
        self.api.register_agent(self.rt, "scout", "codex", None, None)
        self.assertEqual(self.moshi.calls("POST", "/api/register/agents")[0]["body"],
                         {"name": "scout", "harness": "codex"})

    def test_register_agent_reports_a_taken_name(self):
        self.moshi.add_agent("Scout")
        error = self.error_of(self.api.register_agent, self.rt, "scout", "codex", None, None)
        self.assertEqual((error.reason, error.status), ("name_taken", 409))

    def test_load_agent_gives_a_token_for_this_installation(self):
        self.moshi.add_agent("scout", installation_id="inst_other")
        agent, token = self.api.load_agent(self.rt, "scout")
        self.assertEqual(agent["name"], "scout")
        self.assertIn("active_installation", agent)
        self.assertEqual(self.moshi.agent_tokens[token]["installation_id"], self.installation["id"])

    def test_load_agent_errors(self):
        self.assertEqual(self.error_of(self.api.load_agent, self.rt, "ghost").reason, "not_found")
        self.moshi.add_agent("remote", kind="remote")
        self.assertEqual(self.error_of(self.api.load_agent, self.rt, "remote").reason, "not_local")

    def test_load_agent_refuses_a_name_that_is_not_a_moshi_name(self):
        error = self.error_of(self.api.load_agent, self.rt, "../admin")
        self.assertEqual((error.reason, error.status), ("invalid_name", 0))
        self.assertEqual(self.moshi.requests, [])

    def test_adopt_binds_a_legacy_token_to_this_installation(self):
        legacy = self.moshi.add_agent("old", harness="vibe")
        agent = self.api.adopt(self.rt, legacy, "codex")
        self.assertEqual(agent, {"name": "old", "kind": "local", "harness": "codex"})
        self.assertEqual(self.moshi.calls("POST", "/api/register/agents/adopt")[0]["body"],
                         {"token": legacy, "harness": "codex"})
        error = self.error_of(self.api.adopt, self.rt, legacy, "codex")
        self.assertEqual((error.reason, error.status), ("already_adopted", 409))
        self.assertNotIn(legacy, str(error))

    def test_activate_returns_the_previous_installation(self):
        _, token = self.api.register_agent(self.rt, "scout", "codex", None, None)
        self.assertIsNone(self.api.activate(token))
        self.moshi.agent("scout")["active_installation"] = "Home Mac"
        self.assertEqual(self.api.activate(token), "Home Mac")
        self.assertEqual(self.moshi.agent("scout")["active_installation"], "Work Mac")
        self.assertIsNone(self.moshi.calls("POST", "/api/agent/activate")[0]["body"])

    def test_activate_with_a_legacy_token(self):
        legacy = self.moshi.add_agent("old")
        error = self.error_of(self.api.activate, legacy)
        self.assertEqual((error.reason, error.status), ("legacy_token", 409))

    def test_update_profile(self):
        _, token = self.api.register_agent(self.rt, "scout", "codex", "a", "b")
        self.assertIsNone(self.api.update_profile(token, "reviewer", None))
        self.assertEqual(self.moshi.calls("PATCH", "/api/agent/profile")[0]["body"], {"role": "reviewer"})
        self.assertEqual((self.moshi.agent("scout")["role"], self.moshi.agent("scout")["skill"]), ("reviewer", "b"))

    def test_drop_token(self):
        _, token = self.api.register_agent(self.rt, "scout", "codex", None, None)
        self.assertIsNone(self.api.drop_token(token))
        self.assertNotIn(token, self.moshi.agent_tokens)
        self.assertEqual(self.error_of(self.api.drop_token, token).reason, "invalid_token")

    def test_base_url_without_mcp_suffix_works_too(self):
        self.assertEqual(MoshiApi(self.moshi.origin).installation_status(self.device_key)["installation"]["label"],
                         "Work Mac")

    def test_unreachable_server_gives_reason_unreachable(self):
        with socket.socket() as sock:
            sock.bind(("127.0.0.1", 0))
            port = sock.getsockname()[1]  # closed again before the call: nothing listens there
        error = self.error_of(MoshiApi(f"http://127.0.0.1:{port}/mcp", timeout=2).installation_status, self.device_key)
        self.assertEqual((error.reason, error.status), ("unreachable", 0))

    def test_error_text_never_contains_the_bearer(self):
        error = self.error_of(self.api.installation_status, "dk_SECRET_DEVICE_KEY_123")
        self.assertNotIn("SECRET_DEVICE_KEY", str(error))
        self.assertNotIn("SECRET_DEVICE_KEY", repr(error))

    def test_a_server_echoing_the_token_does_not_leak_it_into_the_error(self):
        echo = start_server(self, EchoTokenHandler)
        error = self.error_of(MoshiApi(echo).activate, "bt_ECHOEDSECRET")
        self.assertNotIn("echoedsecret", str(error).lower())
        self.assertNotIn("echoedsecret", error.reason.lower())
        self.assertEqual(error.status, 401)

    def test_a_non_json_answer_is_an_error_without_the_token(self):
        broken = start_server(self, NotJsonHandler)
        error = self.error_of(MoshiApi(broken).installation_status, "dk_NOTJSONSECRET")
        self.assertEqual(error.reason, "bad_response")
        self.assertNotIn("NOTJSONSECRET", str(error))


class RedirectTest(unittest.TestCase):
    """A 3xx is refused, never followed: urllib would forward the Authorization header to any origin
    (it follows a 302 even for a POST, turning it into a GET with the same headers)."""

    def setUp(self):
        self.moshi = serve(self)
        self.other = serve(self)  # a second origin; it must never see a request
        self.moshi.redirect_to = self.other.origin

    def test_moshi_api_refuses_a_cross_origin_redirect(self):
        for status in (302, 307):
            self.moshi.redirect_status = status
            with self.subTest(status=status), self.assertRaises(MoshiError) as ctx:
                MoshiApi(self.moshi.url).installation_status("dk_REDIRECTSECRET")
            self.assertEqual((ctx.exception.reason, ctx.exception.status), ("redirect", status))
            self.assertNotIn("REDIRECTSECRET", str(ctx.exception))
        self.assertEqual(len(self.moshi.requests), 2)
        self.assertEqual(self.other.requests, [])

    def test_moshi_api_refuses_redirects_for_every_bearer_call(self):
        api = MoshiApi(self.moshi.url)
        calls = [(api.device_authorize, "Mac", "dk_x"), (api.register_agent, "rt_x", "n", "codex", None, None),
                 (api.load_agent, "rt_x", "n"), (api.adopt, "rt_x", "bt_y", "codex"), (api.activate, "bt_x"),
                 (api.update_profile, "bt_x", "r", None), (api.drop_token, "bt_x")]
        for status in (301, 302, 303, 307, 308):
            self.moshi.redirect_status = status
            for call, *args in calls:
                with self.subTest(status=status, call=call.__name__), self.assertRaises(MoshiError) as ctx:
                    call(*args)
                self.assertEqual((ctx.exception.reason, ctx.exception.status), ("redirect", status))
        self.assertEqual(self.other.requests, [])

    def test_mcp_client_refuses_a_cross_origin_redirect(self):
        for status in (302, 307):
            self.moshi.redirect_status = status
            with self.subTest(status=status), self.assertRaises(McpError) as ctx:
                McpClient(self.moshi.url, "bt_MCPREDIRECTSECRET").call_tool("mesh_status")
            self.assertIn("redirect", str(ctx.exception))
            self.assertIn(str(status), str(ctx.exception))
            self.assertNotIn("MCPREDIRECTSECRET", str(ctx.exception))
        self.assertEqual(self.other.requests, [])


class EchoTokenHandler(BaseHTTPRequestHandler):
    """A hostile server that puts the caller's token into its error code."""

    def do_POST(self):  # noqa: N802 (http.server API)
        token = (self.headers.get("Authorization") or "")[len("Bearer "):]
        data = json.dumps({"error": token.lower(), "message": token}).encode()
        self.send_response(401)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, *args):
        pass


class NotJsonHandler(BaseHTTPRequestHandler):
    def do_GET(self):  # noqa: N802 (http.server API)
        data = b"<html>gateway</html>"
        self.send_response(200)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, *args):
        pass


def start_server(test_case, handler) -> str:
    server = HTTPServer(("127.0.0.1", 0), handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    test_case.addCleanup(server.server_close)
    test_case.addCleanup(server.shutdown)
    return f"http://127.0.0.1:{server.server_port}/mcp"


if __name__ == "__main__":
    unittest.main()
