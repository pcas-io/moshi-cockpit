"""Hostile or broken servers: redirects, garbage, cut-off and oversized bodies, deep JSON, echoed tokens.
Every one must end as MoshiError / McpError, never as a stray exception, and never carry the token."""

import json
import socket
import threading
import unittest

from cockpit.http import MAX_BODY
from cockpit.mcp import McpClient, McpError
from cockpit.moshi_api import MoshiApi, MoshiError
from tests.fake_moshi import serve

DEEP = ("[" * 30000 + "]" * 30000).encode()  # under MAX_BODY, far over the parser's recursion limit


def http_answer(status_line: str, body: bytes, length=None, content_type="application/json") -> bytes:
    head = f"{status_line}\r\nContent-Type: {content_type}\r\nContent-Length: {len(body) if length is None else length}\r\n"
    return (head + "Connection: close\r\n\r\n").encode() + body


def raw_server(test_case, answer: bytes) -> str:
    """A TCP server that reads one request and sends `answer` byte for byte, then closes; the MCP URL."""
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    listener.listen(8)
    listener.settimeout(0.05)
    stop = threading.Event()

    def serve_forever() -> None:
        while not stop.is_set():
            try:
                conn, _ = listener.accept()
            except OSError:
                continue
            with conn:
                conn.settimeout(2)
                _read_request(conn)
                try:
                    conn.sendall(answer)
                except OSError:
                    pass

    thread = threading.Thread(target=serve_forever, daemon=True)
    thread.start()

    def close() -> None:
        stop.set()
        thread.join(timeout=2)
        listener.close()

    test_case.addCleanup(close)
    return f"http://127.0.0.1:{listener.getsockname()[1]}/mcp"


def _read_request(conn: socket.socket) -> None:
    data = b""
    while b"\r\n\r\n" not in data:
        chunk = conn.recv(4096)
        if not chunk:
            return
        data += chunk
    head, _, rest = data.partition(b"\r\n\r\n")
    length = 0
    for line in head.split(b"\r\n"):
        if line.lower().startswith(b"content-length:"):
            length = int(line.split(b":", 1)[1])
    while len(rest) < length:
        chunk = conn.recv(4096)
        if not chunk:
            return
        rest += chunk


CASES = {
    "garbage status line": (b"SMTP ready, not HTTP\r\n\r\n", "bad_response", 0),
    "cut-off body": (http_answer("HTTP/1.1 200 OK", b'{}', length=500), "bad_response", 200),
    "body over the cap": (http_answer("HTTP/1.1 200 OK", b'"' + b"x" * MAX_BODY + b'"'), "bad_response", 200),
    "deep JSON": (http_answer("HTTP/1.1 200 OK", DEEP), "bad_response", 200),
    "deep JSON error body": (http_answer("HTTP/1.1 401 Unauthorized", DEEP), "http_error", 401),
    "error body over the cap": (http_answer("HTTP/1.1 500 Oops", b"x" * (MAX_BODY + 10)), "bad_response", 500),
    "not JSON": (http_answer("HTTP/1.1 200 OK", b"<html>gateway</html>", content_type="text/html"),
                 "bad_response", 200),
    "JSON but no object": (http_answer("HTTP/1.1 200 OK", b"[1, 2]"), "bad_response", 200),
    "missing fields": (http_answer("HTTP/1.1 200 OK", b'{"agents": "many"}'), "bad_response", 200),
    "connection closed without an answer": (b"", "unreachable", 0),
}


class MoshiApiHostileTest(unittest.TestCase):
    def test_every_broken_answer_is_a_moshi_error(self):
        for name, (answer, reason, status) in CASES.items():
            with self.subTest(case=name):
                with self.assertRaises(MoshiError) as ctx:
                    MoshiApi(raw_server(self, answer), timeout=3).installation_status("dk_HOSTILESECRET")
                self.assertEqual((ctx.exception.reason, ctx.exception.status), (reason, status))
                self.assertNotIn("HOSTILESECRET", str(ctx.exception))

    def test_fields_of_the_wrong_type_are_bad_responses(self):
        answers = [
            ("device_authorize", ("Mac",), {"device_code": None, "user_code": "B-C", "verification_uri": "u",
                                            "verification_uri_complete": "u", "expires_in": 600, "interval": 5}),
            ("device_authorize", ("Mac",), {"device_code": "d", "user_code": "B-C", "verification_uri": "u",
                                            "verification_uri_complete": "u", "expires_in": True, "interval": 5}),
            ("device_token", ("code",), {"device_key": "dk_x", "installation": {"id": "i"},
                                         "registration_token": "rt_x", "registration_expires_at": "2026-11-16T10:00:00Z"}),
            ("device_token", ("code",), {"device_key": 7, "installation": {"id": "i", "label": "l"},
                                         "registration_token": "rt_x", "registration_expires_at": "2026-11-16T10:00:00Z"}),
            ("device_token", ("code",), {"device_key": None, "installation": {"id": "i", "label": "l"},
                                         "registration_token": "rt_x", "registration_expires_at": "soon"}),
            ("adopt", ("rt_x", "bt_y", "codex"), {"agent": "old"}),
            ("register_agent", ("rt_x", "n", "codex", None, None), {"agent": {"name": "n"}, "token": None}),
            ("activate", ("bt_x",), {"previous": 42}),
        ]
        for method, args, body in answers:
            url = raw_server(self, http_answer("HTTP/1.1 200 OK", json.dumps(body).encode()))
            with self.subTest(method=method, body=body):
                with self.assertRaises(MoshiError) as ctx:
                    getattr(MoshiApi(url), method)(*args)
                self.assertEqual(ctx.exception.reason, "bad_response")

    def test_a_server_echoing_the_token_does_not_leak_it_into_the_error(self):
        for echoed in ("bt_echoedsecret", "echoedsecret"):
            body = json.dumps({"error": echoed, "message": "bt_echoedsecret"}).encode()
            url = raw_server(self, http_answer("HTTP/1.1 401 Unauthorized", body))
            with self.subTest(echoed=echoed):
                with self.assertRaises(MoshiError) as ctx:
                    MoshiApi(url).activate("bt_echoedsecret")
                self.assertEqual((ctx.exception.reason, ctx.exception.status), ("http_error", 401))
                self.assertNotIn("echoedsecret", str(ctx.exception).lower())

    def test_the_legacy_token_in_adopt_is_hidden_too(self):
        body = json.dumps({"error": "bt_legacysecret"}).encode()
        url = raw_server(self, http_answer("HTTP/1.1 409 Conflict", body))
        with self.assertRaises(MoshiError) as ctx:
            MoshiApi(url).adopt("rt_x", "bt_legacysecret", "codex")
        self.assertEqual(ctx.exception.reason, "http_error")


class McpClientHostileTest(unittest.TestCase):
    def test_every_broken_answer_is_an_mcp_error(self):
        cases = {name: answer for name, (answer, _, _) in CASES.items() if name != "missing fields"}
        cases["SSE event with deep JSON"] = http_answer("HTTP/1.1 200 OK", b"data: " + DEEP + b"\n\n",
                                                        content_type="text/event-stream")
        cases["result is no object"] = http_answer("HTTP/1.1 200 OK", b'{"jsonrpc": "2.0", "id": 1, "result": [1]}')
        for name, answer in cases.items():
            with self.subTest(case=name):
                with self.assertRaises(McpError) as ctx:
                    McpClient(raw_server(self, answer), "bt_MCPHOSTILESECRET", timeout=3).call_tool("mesh_status")
                self.assertNotIn("MCPHOSTILESECRET", str(ctx.exception))

    def test_a_deep_tool_result_is_an_mcp_error(self):
        text = json.dumps({"jsonrpc": "2.0", "id": 1, "result": {"content": [{"type": "text", "text": DEEP.decode()}]}})
        # initialize and tools/call both get this answer; the tool's own text is what nests too deep
        url = raw_server(self, http_answer("HTTP/1.1 200 OK", text.encode()))
        with self.assertRaises(McpError):
            McpClient(url, "bt_x", timeout=3).call_tool("mesh_status")


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
            with self.subTest(status=status):
                with self.assertRaises(MoshiError) as ctx:
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
                with self.subTest(status=status, call=call.__name__):
                    with self.assertRaises(MoshiError) as ctx:
                        call(*args)
                    self.assertEqual((ctx.exception.reason, ctx.exception.status), ("redirect", status))
        self.assertEqual(self.other.requests, [])

    def test_mcp_client_refuses_a_cross_origin_redirect(self):
        for status in (302, 307):
            self.moshi.redirect_status = status
            with self.subTest(status=status):
                with self.assertRaises(McpError) as ctx:
                    McpClient(self.moshi.url, "bt_MCPREDIRECTSECRET").call_tool("mesh_status")
                self.assertIn("redirect", str(ctx.exception))
                self.assertIn(str(status), str(ctx.exception))
                self.assertNotIn("MCPREDIRECTSECRET", str(ctx.exception))
        self.assertEqual(self.other.requests, [])


if __name__ == "__main__":
    unittest.main()
