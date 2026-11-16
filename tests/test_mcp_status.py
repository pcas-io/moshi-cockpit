import json
import threading
import unittest
from http.server import BaseHTTPRequestHandler, HTTPServer

from cockpit import status
from cockpit.keychain import Keychain
from cockpit.mcp import McpClient, McpError, parse_body, probe_unauthenticated
from tests.helpers import FakeSecurity, bind_cockpit, configure, temp_paths

TOKEN = "bt_TESTTOKEN_abcdefgh"


class FakeMcp(BaseHTTPRequestHandler):
    """Answers like moshi: initialize as SSE with a session id, tools/call as JSON."""

    seen_auth = []

    def do_POST(self):  # noqa: N802 (http.server API)
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        FakeMcp.seen_auth.append(self.headers.get("Authorization"))
        if self.headers.get("Authorization") != f"Bearer {TOKEN}":
            self.send_response(401)
            self.send_header("WWW-Authenticate", 'Bearer resource_metadata="x"')
            self.end_headers()
            return
        if "id" not in body:
            self.send_response(202)
            self.end_headers()
            return
        if body["method"] == "initialize":
            payload = {"jsonrpc": "2.0", "id": body["id"], "result": {"protocolVersion": "2025-06-18"}}
            self._send("text/event-stream", f"event: message\ndata: {json.dumps(payload)}\n\n", session="s1")
            return
        agents = {"agents": [{"name": "vibe", "presence": "live", "role": "skill-a", "working_on": "idle",
                              "last_seen_at": "2026-11-07T09:00:00Z"},
                             {"name": "Grok", "presence": "stale", "working_on": "delivering: x to y"}]}
        result = {"content": [{"type": "text", "text": json.dumps(agents)}], "isError": False}
        self._send("application/json", json.dumps({"jsonrpc": "2.0", "id": body["id"], "result": result}))

    def _send(self, content_type, text, session=None):
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        if session:
            self.send_header("Mcp-Session-Id", session)
        self.end_headers()
        self.wfile.write(text.encode())

    def log_message(self, *args):
        pass


class McpTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = HTTPServer(("127.0.0.1", 0), FakeMcp)
        cls.url = f"http://127.0.0.1:{cls.server.server_port}/mcp"
        threading.Thread(target=cls.server.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()

    def test_call_tool_handles_sse_init_and_json_result(self):
        result = McpClient(self.url, TOKEN).call_tool("mesh_status")
        self.assertEqual(result["agents"][0]["name"], "vibe")

    def test_wrong_token_raises_without_leaking_it(self):
        with self.assertRaises(McpError) as ctx:
            McpClient(self.url, "bt_WRONGTOKEN_123456").call_tool("mesh_status")
        self.assertNotIn("WRONGTOKEN", str(ctx.exception))

    def test_probe_unauthenticated_sees_bearer_challenge(self):
        code, challenge = probe_unauthenticated(self.url)
        self.assertEqual(code, 401)
        self.assertTrue(challenge.startswith("Bearer"))

    def test_status_end_to_end_with_keychain(self):
        keychain = Keychain(FakeSecurity({("moshi-cockpit", "cockpit"): TOKEN}))
        paths = temp_paths(self)
        configure(paths, servers={})
        bind_cockpit(paths, self.url)
        identity = status.pick_identity(keychain, None)
        agents = status.fetch_agents(keychain, identity, self.url, paths,
                                     client_factory=lambda _url, tok: McpClient(self.url, tok))
        text = status.render(agents, identity)
        self.assertIn("vibe", text)
        self.assertNotIn("Hinweis", text)

    def test_status_warns_when_reading_as_an_agent(self):
        keychain = Keychain(FakeSecurity({("moshi-vibe", "vibe"): TOKEN}))
        identity = status.pick_identity(keychain, None, ["vibe"])
        self.assertEqual(identity, "vibe")
        self.assertIn("Hinweis", status.render([], identity))


class RenderSafetyTest(unittest.TestCase):
    def test_control_characters_from_server_are_stripped(self):
        hostile = {"name": "evil\x1b]0;pwned\x07", "presence": "live", "role": "x\x1b[2J",
                   "working_on": "line1\r\nline2\x1b]8;;http://phish\x07click\x1b]8;;\x07"}
        text = status.render([hostile], "cockpit")
        self.assertNotIn("\x1b", text)
        self.assertNotIn("\x07", text)
        self.assertNotIn("\r", text)


class ErrorSanitizingTest(unittest.TestCase):
    def test_server_error_text_is_cleaned_before_it_reaches_the_terminal(self):
        class Response:
            status = 200
            headers = {"Content-Type": "application/json", "Mcp-Session-Id": "s"}

            def __init__(self, payload):
                self._payload = payload

            def read(self):
                return json.dumps(self._payload).encode()

            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return False

        replies = iter([{"jsonrpc": "2.0", "id": 1, "result": {}}, {},
                        {"jsonrpc": "2.0", "id": 2, "error": {"message": "boom\x1b]0;owned\x07\r\nfake"}}])
        client = McpClient("http://x/mcp", TOKEN, opener=lambda *a, **k: Response(next(replies)))
        with self.assertRaises(McpError) as ctx:
            client.call_tool("mesh_status")
        self.assertNotIn("\x1b", str(ctx.exception))
        self.assertNotIn("\r", str(ctx.exception))


class ParseBodyTest(unittest.TestCase):
    def test_last_sse_event_wins(self):
        body = 'data: {"a": 1}\n\ndata: {"a": 2}\n\n'
        self.assertEqual(parse_body("text/event-stream", body), {"a": 2})

    def test_invalid_json_raises(self):
        with self.assertRaises(McpError):
            parse_body("application/json", "<html>")


if __name__ == "__main__":
    unittest.main()
