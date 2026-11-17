"""The unauthenticated probe doctor uses: a healthy endpoint answers 401 with a Bearer challenge."""

import threading
import unittest
from http.server import BaseHTTPRequestHandler, HTTPServer

from cockpit.mcp import McpError, probe_unauthenticated


class FakeMcp(BaseHTTPRequestHandler):
    """Answers every request without a token like moshi does: 401 with a Bearer challenge."""

    def do_POST(self):  # noqa: N802 (http.server API)
        self.rfile.read(int(self.headers["Content-Length"]))
        self.send_response(401)
        self.send_header("WWW-Authenticate", 'Bearer resource_metadata="x"')
        self.end_headers()

    def log_message(self, *args):
        pass


class ProbeTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = HTTPServer(("127.0.0.1", 0), FakeMcp)
        cls.url = f"http://127.0.0.1:{cls.server.server_port}/mcp"
        threading.Thread(target=cls.server.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()

    def test_probe_unauthenticated_sees_bearer_challenge(self):
        code, challenge = probe_unauthenticated(self.url)
        self.assertEqual(code, 401)
        self.assertTrue(challenge.startswith("Bearer"))

    def test_an_unreachable_endpoint_is_an_mcp_error(self):
        import socket
        with socket.socket() as probe:
            probe.bind(("127.0.0.1", 0))
            port = probe.getsockname()[1]
        with self.assertRaises(McpError) as ctx:
            probe_unauthenticated(f"http://127.0.0.1:{port}/mcp")
        self.assertIn("is not reachable", str(ctx.exception))


if __name__ == "__main__":
    unittest.main()
