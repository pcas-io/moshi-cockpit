"""MoshiApi against FakeMoshi (which answers like moshi 1.4.0): one test per call and its refusals."""

import socket
import unittest
from datetime import datetime, timezone

from cockpit.config import moshi_base
from cockpit.moshi_api import DeviceGrant, DeviceStart, MoshiApi, MoshiError, parse_timestamp
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

    def assertError(self, error, reason, status):  # noqa: N802 (unittest style)
        self.assertEqual((error.reason, error.status), (reason, status))

    # -- device flow ------------------------------------------------------------------------------

    def test_device_authorize_starts_a_flow(self):
        start = self.api.device_authorize("Work Mac")
        self.assertIsInstance(start, DeviceStart)
        self.assertRegex(start.user_code, r"^[B-Z]{4}-[B-Z]{4}$")
        self.assertEqual(start.verification_uri, self.moshi.origin + "/link")
        self.assertEqual(start.verification_uri_complete, f"{self.moshi.origin}/link?code={start.user_code}")
        self.assertEqual((start.expires_in, start.interval), (600, 5))
        call = self.moshi.calls("POST", "/api/device/authorize")[0]
        self.assertEqual(call["body"], {"label": "Work Mac"})
        self.assertIsNone(call["auth"])

    def test_device_authorize_with_an_empty_label_is_unnamed(self):
        start = self.api.device_authorize("  ")
        self.assertEqual(self.moshi.flow_by_user_code(start.user_code.replace("-", ""))["label"], "unnamed")

    def test_device_authorize_renewal_sends_the_device_key_and_keeps_the_label(self):
        start = self.api.device_authorize("Another name", device_key=self.device_key)
        self.assertEqual(self.moshi.calls("POST", "/api/device/authorize")[0]["auth"], f"Bearer {self.device_key}")
        self.assertEqual(self.moshi.flow_by_user_code(start.user_code.replace("-", ""))["label"], "Work Mac")

    def test_device_authorize_refusals(self):
        self.assertError(self.error_of(self.api.device_authorize, "Mac", "dk_revoked"), "invalid_device_key", 401)
        for _ in range(9):  # 10 per address per 10 minutes, the refused renewal above counted too
            self.api.device_authorize("Mac")
        self.assertError(self.error_of(self.api.device_authorize, "Mac"), "too_many_requests", 429)

    def test_device_token_reports_each_pending_state_as_a_string(self):
        start = self.api.device_authorize("Work Mac")
        code = start.user_code.replace("-", "")
        self.assertEqual(self.api.device_token(start.device_code), "authorization_pending")
        self.moshi.slow_down = 1
        self.assertEqual(self.api.device_token(start.device_code), "slow_down")
        self.moshi.deny(code)
        self.assertEqual(self.api.device_token(start.device_code), "access_denied")
        self.moshi.expire(code)
        self.assertEqual(self.api.device_token(start.device_code), "expired_token")

    def test_device_token_without_a_code_is_invalid_request(self):
        self.assertError(self.error_of(self.api.device_token, ""), "invalid_request", 400)

    def test_device_token_returns_the_grant_once(self):
        start = self.api.device_authorize("Home Mac")
        self.moshi.approve(start.user_code.replace("-", ""))
        grant = self.api.device_token(start.device_code)
        self.assertIsInstance(grant, DeviceGrant)
        self.assertTrue(grant.device_key.startswith("dk_"))
        self.assertRegex(grant.registration_token, r"^rt_[0-9a-z]{26}_[A-Za-z0-9_-]{43}$")
        self.assertEqual(grant.installation_label, "Home Mac")
        self.assertEqual(self.moshi.installations[grant.installation_id]["label"], "Home Mac")
        self.assertRegex(grant.registration_expires_at, r"^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d\.\d{3}Z$")
        self.assertGreater(parse_timestamp(grant.registration_expires_at), datetime.now(timezone.utc))
        self.assertEqual(self.api.device_token(start.device_code), "expired_token")  # redeemed exactly once

    def test_device_token_on_renewal_has_no_new_device_key_and_ends_the_old_registration(self):
        start = self.api.device_authorize("Work Mac", device_key=self.device_key)
        self.moshi.approve(start.user_code.replace("-", ""))
        grant = self.api.device_token(start.device_code)
        self.assertIsNone(grant.device_key)
        self.assertEqual(grant.installation_id, self.installation["id"])
        self.assertError(self.error_of(self.api.register_agent, self.rt, "scout", "codex", None, None),
                         "token_revoked", 401)
        self.api.register_agent(grant.registration_token, "scout", "codex", None, None)

    # -- installation status ----------------------------------------------------------------------

    def test_installation_status_with_the_device_key(self):
        self.moshi.add_agent("vibe", role="reviewer", presence="live")
        self.moshi.add_agent("gone", active=False)
        status = self.api.installation_status(self.device_key)
        self.assertEqual(status["installation"], {"id": self.installation["id"], "label": "Work Mac"})
        self.assertEqual([a["name"] for a in status["agents"]], ["vibe"])  # inactive agents are left out
        vibe = status["agents"][0]
        self.assertEqual((vibe["presence"], vibe["role"], vibe["skill"], vibe["working_on"]),
                         ("live", "reviewer", None, None))
        self.assertEqual(self.moshi.calls("GET", "/api/installation/status")[0]["auth"], f"Bearer {self.device_key}")

    def test_installation_status_with_a_revoked_device_key_is_401(self):
        self.moshi.revoke_installation(self.installation["id"])
        self.assertError(self.error_of(self.api.installation_status, self.device_key), "http_error", 401)

    def test_installation_status_needs_a_device_key(self):
        _, token = self.api.register_agent(self.rt, "scout", "codex", None, None)
        self.assertError(self.error_of(self.api.installation_status, token), "device_key_required", 403)

    def test_a_device_key_works_nowhere_else(self):
        self.assertError(self.error_of(self.api.activate, self.device_key), "device_key_scope", 403)

    # -- registration -----------------------------------------------------------------------------

    def test_register_agent_returns_a_local_agent_held_here_and_its_token(self):
        agent, token = self.api.register_agent(self.rt, "scout", "codex", "reviewer", "triage")
        self.assertEqual(agent["kind"], "local")
        self.assertEqual((agent["name"], agent["harness"], agent["role"], agent["skill"]),
                         ("scout", "codex", "reviewer", "triage"))
        self.assertTrue(token.startswith("bt_"))
        self.assertEqual(self.moshi.agent("scout")["active_installation_id"], self.installation["id"])
        call = self.moshi.calls("POST", "/api/register/agents")[0]
        self.assertEqual(call["auth"], f"Bearer {self.rt}")
        self.assertEqual(call["body"], {"name": "scout", "harness": "codex", "role": "reviewer", "skill": "triage"})

    def test_register_agent_leaves_out_missing_role_and_skill(self):
        agent, _ = self.api.register_agent(self.rt, "scout", "codex", None, None)
        self.assertEqual((agent["role"], agent["skill"]), (None, None))
        self.assertEqual(self.moshi.calls("POST", "/api/register/agents")[0]["body"],
                         {"name": "scout", "harness": "codex"})

    def test_register_agent_refusals(self):
        self.moshi.add_agent("Scout")
        cases = [(("scout", "codex"), "name_taken", 409), (("scout", "emacs"), "invalid_profile", 400),
                 (("-bad", "codex"), "invalid_name", 400), (("admin", "codex"), "registration_failed", 400)]
        for (name, harness), reason, status in cases:
            with self.subTest(reason=reason):
                self.assertError(self.error_of(self.api.register_agent, self.rt, name, harness, None, None),
                                 reason, status)

    def test_register_agent_rate_limit(self):
        for index in range(10):
            self.api.register_agent(self.rt, f"a{index}", "codex", None, None)
        self.assertError(self.error_of(self.api.register_agent, self.rt, "a10", "codex", None, None),
                         "rate_limited", 429)

    def test_registration_token_refusals(self):
        expired = self.moshi.add_registration_token(self.installation["id"], expired=True)
        self.assertError(self.error_of(self.api.register_agent, expired, "a", "codex", None, None), "token_expired", 401)
        fresh = self.moshi.add_registration_token(self.installation["id"])
        self.assertError(self.error_of(self.api.load_agent, self.rt, "a"), "token_revoked", 401)  # replaced
        self.assertError(self.error_of(self.api.load_agent, "rt_short", "a"), "invalid_token", 401)
        self.assertError(self.error_of(self.api.load_agent, self.device_key, "a"), "registration_token_required", 401)
        self.moshi.revoke_installation(self.installation["id"])
        self.assertError(self.error_of(self.api.load_agent, fresh, "a"), "token_revoked", 401)

    def test_load_agent_gives_a_token_for_this_installation_and_keeps_the_holder(self):
        other, _ = self.moshi.add_installation("Home Mac")
        self.moshi.add_agent("scout", installation_id=other["id"])
        self.moshi.agent("scout")["active_installation_id"] = other["id"]
        agent, token = self.api.load_agent(self.rt, "scout")
        self.assertEqual((agent["name"], agent["active_installation"]), ("scout", "Home Mac"))
        self.assertEqual(self.moshi.agent_tokens[token]["installation_id"], self.installation["id"])
        self.assertEqual(self.moshi.agent("scout")["active_installation_id"], other["id"])
        _, again = self.api.load_agent(self.rt, "scout")
        self.assertTrue(self.moshi.agent_tokens[token]["revoked"])  # one live token per installation
        self.assertFalse(self.moshi.agent_tokens[again]["revoked"])

    def test_load_agent_refusals(self):
        self.assertError(self.error_of(self.api.load_agent, self.rt, "ghost"), "not_found", 404)
        self.moshi.add_agent("hosted", kind="hosted")
        self.assertError(self.error_of(self.api.load_agent, self.rt, "hosted"), "not_local", 409)

    def test_load_agent_refuses_a_name_that_is_not_a_moshi_name(self):
        self.assertError(self.error_of(self.api.load_agent, self.rt, "../admin"), "invalid_name", 0)
        self.assertEqual(self.moshi.requests, [])

    def test_adopt_moves_a_legacy_token_here_and_holds_the_agent(self):
        legacy = self.moshi.add_agent("old", kind="hosted", harness=None)
        agent = self.api.adopt(self.rt, legacy, "codex")
        self.assertEqual(agent, {"name": "old", "kind": "local", "harness": "codex"})
        self.assertEqual(self.moshi.calls("POST", "/api/register/agents/adopt")[0]["body"],
                         {"token": legacy, "harness": "codex"})
        self.assertEqual(self.moshi.agent("old")["active_installation_id"], self.installation["id"])
        self.assertEqual(self.api.activate(legacy), "Work Mac")  # the plaintext now is this installation's token
        error = self.error_of(self.api.adopt, self.rt, legacy, "codex")
        self.assertError(error, "already_adopted", 409)
        self.assertNotIn(legacy, str(error))

    def test_adopt_refusals(self):
        inactive = self.moshi.add_agent("idle", active=False)
        cases = [(("bt_nobody", "codex"), "not_found", 404), (("xx_token", "codex"), "invalid_token", 400),
                 ((self.moshi.admin_token, "codex"), "admin_token_refused", 400),
                 ((inactive, "codex"), "agent_inactive", 409), (("bt_nobody", "emacs"), "invalid_profile", 400)]
        for (token, harness), reason, status in cases:
            with self.subTest(reason=reason):
                self.assertError(self.error_of(self.api.adopt, self.rt, token, harness), reason, status)

    # -- the agent's own token --------------------------------------------------------------------

    def test_activate_returns_the_previous_holder_even_when_it_was_this_installation(self):
        _, token = self.api.register_agent(self.rt, "scout", "codex", None, None)
        self.assertEqual(self.api.activate(token), "Work Mac")  # register already made this one active
        other, _ = self.moshi.add_installation("Home Mac")
        self.moshi.agent("scout")["active_installation_id"] = other["id"]
        self.assertEqual(self.api.activate(token), "Home Mac")
        self.assertEqual(self.moshi.agent("scout")["active_installation_id"], self.installation["id"])
        self.moshi.agent("scout")["active_installation_id"] = None
        self.assertIsNone(self.api.activate(token))
        self.assertIsNone(self.moshi.calls("POST", "/api/agent/activate")[0]["body"])

    def test_activate_with_a_legacy_token(self):
        legacy = self.moshi.add_agent("old")
        self.assertError(self.error_of(self.api.activate, legacy), "legacy_token", 409)

    def test_update_profile(self):
        _, token = self.api.register_agent(self.rt, "scout", "codex", "a", "b")
        self.assertIsNone(self.api.update_profile(token, "reviewer", None))
        self.assertEqual(self.moshi.calls("PATCH", "/api/agent/profile")[0]["body"], {"role": "reviewer"})
        self.assertEqual((self.moshi.agent("scout")["role"], self.moshi.agent("scout")["skill"]), ("reviewer", "b"))
        self.api.update_profile(token, "", None)
        self.assertIsNone(self.moshi.agent("scout")["role"])  # empty clears it
        self.api.update_profile(token, None, "")
        self.assertEqual(self.moshi.calls("PATCH", "/api/agent/profile")[2]["body"], {"skill": None})
        self.assertIsNone(self.moshi.agent("scout")["skill"])  # sent as null: moshi refuses an empty name
        self.assertError(self.error_of(self.api.update_profile, token, None, "no spaces"), "invalid_profile", 400)

    def test_drop_token_ends_it_and_releases_the_agent(self):
        _, token = self.api.register_agent(self.rt, "scout", "codex", None, None)
        self.assertIsNone(self.api.drop_token(token))
        self.assertIsNone(self.moshi.agent("scout")["active_installation_id"])
        self.assertError(self.error_of(self.api.drop_token, token), "http_error", 401)  # the middleware's 401

    # -- plumbing ---------------------------------------------------------------------------------

    def test_base_url_variants_reach_the_origin(self):
        for base in (self.moshi.origin, self.moshi.origin + "/", self.moshi.url + "/", self.moshi.origin + "/x/mcp"):
            with self.subTest(base=base):
                self.assertEqual(MoshiApi(base).installation_status(self.device_key)["installation"]["label"],
                                 "Work Mac")

    def test_unreachable_server_gives_reason_unreachable(self):
        with socket.socket() as sock:
            sock.bind(("127.0.0.1", 0))
            port = sock.getsockname()[1]  # closed again before the call: nothing listens there
        error = self.error_of(MoshiApi(f"http://127.0.0.1:{port}/mcp", timeout=2).installation_status, self.device_key)
        self.assertError(error, "unreachable", 0)

    def test_error_text_never_contains_the_bearer(self):
        error = self.error_of(self.api.installation_status, "dk_SECRET_DEVICE_KEY_123")
        self.assertNotIn("SECRET_DEVICE_KEY", str(error))
        self.assertNotIn("SECRET_DEVICE_KEY", repr(error))


class HelpersTest(unittest.TestCase):
    def test_the_user_agent_names_this_release(self):
        import cockpit
        from cockpit.paths import USER_AGENT
        self.assertEqual(USER_AGENT, f"moshi-cockpit/{cockpit.__version__}")

    def test_moshi_base_is_the_origin(self):
        cases = {"https://moshi.example/mcp": "https://moshi.example",
                 "https://moshi.example/mcp/": "https://moshi.example",
                 "https://moshi.example/team/mcp": "https://moshi.example",
                 "https://moshi.example:8443/mcp": "https://moshi.example:8443",
                 "http://[::1]:8080/mcp": "http://[::1]:8080",
                 "https://moshi.example": "https://moshi.example"}
        for url, expected in cases.items():
            with self.subTest(url=url):
                self.assertEqual(moshi_base(url), expected)
        with self.assertRaises(ValueError):
            moshi_base("moshi.example/mcp")

    def test_parse_timestamp_reads_moshi_forms(self):
        expected = datetime(2026, 11, 16, 10, 0, 0, 123000, tzinfo=timezone.utc)
        self.assertEqual(parse_timestamp("2026-11-16T10:00:00.123Z"), expected)
        self.assertEqual(parse_timestamp("2026-11-16T10:00:00Z"), expected.replace(microsecond=0))
        self.assertEqual(parse_timestamp("2026-11-16T11:00:00.123+01:00"), expected)
        self.assertEqual(parse_timestamp("2026-11-16T10:00:00.123456789Z"), expected.replace(microsecond=123456))
        for bad in ("", "2026-11-16", "2026-11-16T10:00:00", "2026-13-16T10:00:00Z", "yesterday", None):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                parse_timestamp(bad)


if __name__ == "__main__":
    unittest.main()
