"""Agents in moshi: create, load, catalog, set. FakeMoshi behind MoshiApi, fake keychain, no network."""

import io
import json
import unittest
from contextlib import redirect_stderr, redirect_stdout
from datetime import datetime, timedelta, timezone
from unittest import mock

from cockpit import agents_remote, cli, registry
from cockpit.config import update
from cockpit.moshi_api import MoshiApi
from tests.fake_moshi import serve
from tests.helpers import MOSHI, OCELLIO, bind_non_agent, configure, context

OCELLIO_TOKEN = "pt_OCELLIO_default_0123456789"
DEVICE_ENTRY = ("moshi-device", "device")
REGISTER_ENTRY = ("moshi-register", "register")
OCELLIO_ENTRY = ("ocellio-default", "default")


def iso(moment):
    return moment.strftime("%Y-%m-%dT%H:%M:%S.000Z")


class ConnectedHerdr:
    """Mixin: a Herdr connected to moshi (device key, a live registration token, the person's ocellio token)."""

    def setUp(self):
        self.ctx, self.fake = context(self)
        configure(self.ctx.paths)  # moshi and ocellio configured
        self.moshi = serve(self)
        self.installation, self.device_key = self.moshi.add_installation("Work Mac")
        self.rt = self.moshi.add_registration_token(self.installation["id"])
        self.fake.entries.update({DEVICE_ENTRY: self.device_key, REGISTER_ENTRY: self.rt, OCELLIO_ENTRY: OCELLIO_TOKEN})
        self.connected()
        self.api = MoshiApi(self.moshi.url)
        installed = mock.patch("cockpit.harnesses.base.Harness.installed", return_value=True)
        installed.start()
        self.addCleanup(installed.stop)

    def connected(self, agents=None):
        """The config `connect` leaves behind (bindings, expiry), with `agents` in the list when given."""
        if agents is not None:
            configure(self.ctx.paths, agents=agents)
        bind_non_agent(self.ctx.paths, MOSHI, "device")
        bind_non_agent(self.ctx.paths, MOSHI, "register")
        bind_non_agent(self.ctx.paths, OCELLIO, "ocellio-default")
        self.expires(datetime.now(timezone.utc) + timedelta(hours=20))

    def expires(self, moment):
        update(self.ctx.paths, self.ctx.backup, "register_expires_at", iso(moment))

    def live_tokens(self, name):
        return [t for t, e in self.moshi.agent_tokens.items() if e["name"] == name.lower() and not e["revoked"]]

    def cli(self, *argv):
        out, err = io.StringIO(), io.StringIO()
        with mock.patch("cockpit.cli.Paths.from_env", return_value=self.ctx.paths), \
                mock.patch("cockpit.cli.default_store", return_value=self.ctx.keychain), \
                mock.patch("cockpit.agents_remote.api_for", return_value=self.api), \
                redirect_stdout(out), redirect_stderr(err):
            code = cli.main(list(argv))
        return code, out.getvalue(), err.getvalue()

    def assert_no_secret(self, *texts):
        for secret in [self.rt, self.device_key, OCELLIO_TOKEN] + list(self.moshi.agent_tokens):
            for text in texts:
                self.assertNotIn(secret, text)
            self.assertFalse(self.fake.argv_contains(secret))


class RemoteAgentsTest(ConnectedHerdr, unittest.TestCase):
    # -- create -----------------------------------------------------------------------------------

    def test_add_creates_the_agent_in_moshi_and_stores_its_token(self):
        agent = agents_remote.create(self.ctx, "scout", "codex", "Research", "skill-a", api=self.api)
        remote = self.moshi.agent("scout")
        self.assertEqual((remote["kind"], remote["harness"], remote["role"], remote["skill"]),
                         ("local", "codex", "Research", "skill-a"))
        self.assertEqual(remote["active_installation_id"], self.installation["id"])
        self.assertEqual(self.moshi.calls("POST", "/api/register/agents")[0]["auth"], f"Bearer {self.rt}")
        self.assertEqual([self.fake.entries[("moshi-scout", "scout")]], self.live_tokens("scout"))
        self.assertEqual(self.fake.entries[("ocellio-scout", "scout")], OCELLIO_TOKEN)
        self.assertEqual(agent.bound, {"moshi": "https://moshi.example.test", "ocellio": "https://ocellio.example.test"})
        self.assertEqual(registry.find(self.ctx.paths, "scout"), agent)
        self.assert_no_secret()

    def test_add_through_the_cli_prints_no_token(self):
        code, out, err = self.cli("agent", "add", "scout", "--harness=codex", "--role=Research")
        self.assertEqual(code, 0, err)
        self.assertIn("scout", out)
        self.assertIsNotNone(self.moshi.agent("scout"))
        self.assert_no_secret(out, err)

    def test_claude_gets_no_ocellio_copy(self):
        agent = agents_remote.create(self.ctx, "writer", "claude", api=self.api)
        self.assertNotIn(("ocellio-writer", "writer"), self.fake.entries)
        self.assertEqual(set(agent.bound), {"moshi"})

    def test_the_ocellio_token_of_another_server_is_never_copied(self):
        bound = json.loads(self.ctx.paths.mesh_config.read_text())
        bound["bound"]["ocellio-default"] = "https://other.example.test"
        self.ctx.paths.mesh_config.write_text(json.dumps(bound))
        agent = agents_remote.create(self.ctx, "scout", "codex", api=self.api)
        self.assertNotIn(("ocellio-scout", "scout"), self.fake.entries)
        self.assertNotIn("ocellio", agent.bound)

    def test_add_with_an_expired_registration_token_exits_3_without_calling_moshi(self):
        self.expires(datetime.now(timezone.utc) - timedelta(minutes=1))
        code, out, err = self.cli("agent", "add", "scout", "--harness=codex")
        self.assertEqual(code, 3)
        self.assertIn("moshi-cockpit connect", err)
        self.assertEqual(self.moshi.requests, [])
        self.assertEqual(registry.load_agents(self.ctx.paths), [])
        self.assertFalse(any(args[:2] == ["security", "find-generic-password"] and "-w" in args
                             for args, _ in self.fake.calls))  # the registration token was not even read

    def test_add_without_a_registration_token_exits_3(self):
        del self.fake.entries[REGISTER_ENTRY]
        self.assertEqual(self.cli("agent", "add", "scout", "--harness=codex")[0], 3)
        self.assertEqual(self.moshi.requests, [])

    def test_a_registration_token_moshi_refuses_exits_3(self):
        self.moshi.registration_tokens[self.rt]["revoked"] = True  # e.g. renewed on another device
        code, _, err = self.cli("agent", "add", "scout", "--harness=codex")
        self.assertEqual(code, 3, err)
        self.assertEqual(registry.load_agents(self.ctx.paths), [])
        self.assertNotIn(("moshi-scout", "scout"), self.fake.entries)
        self.assertEqual(self.cli("agent", "load", "scout")[0], 3)

    def test_add_reports_a_taken_name(self):
        self.moshi.add_agent("Scout")
        code, out, err = self.cli("agent", "add", "scout", "--harness=codex")
        self.assertEqual(code, 1)
        self.assertIn("That name is taken in moshi.", err)
        self.assertEqual(registry.load_agents(self.ctx.paths), [])
        self.assertNotIn(("moshi-scout", "scout"), self.fake.entries)

    def test_a_local_refusal_never_reaches_moshi(self):
        self.connected(agents=[{"name": "a1", "harness": "agy"}])
        with self.assertRaises(ValueError):
            agents_remote.create(self.ctx, "a2", "agy", api=self.api)  # one agy agent per Mac
        with self.assertRaises(ValueError):
            agents_remote.create(self.ctx, "A1", "codex", api=self.api)  # the name exists here
        self.assertEqual(self.moshi.requests, [])

    def test_a_failure_here_after_moshi_created_the_agent_drops_its_token_and_says_how_to_load_it(self):
        with mock.patch("cockpit.registry.add", side_effect=ValueError("disk full")):
            with self.assertRaises(ValueError) as caught:
                agents_remote.create(self.ctx, "scout", "codex", api=self.api)
        self.assertEqual(str(caught.exception), "disk full. Load it with: moshi-cockpit agent load scout")
        self.assertIsNotNone(self.moshi.agent("scout"))  # it exists in moshi
        self.assertEqual(self.live_tokens("scout"), [])   # this installation's new token ended
        self.assertNotIn(("moshi-scout", "scout"), self.fake.entries)
        self.assertNotIn(("ocellio-scout", "scout"), self.fake.entries)
        agents_remote.load(self.ctx, "scout", api=self.api)  # the way out works
        self.assertEqual(len(self.live_tokens("scout")), 1)

    def test_a_file_error_here_drops_the_new_token_and_ends_in_one_line(self):
        with mock.patch("cockpit.registry.add", side_effect=PermissionError(13, "Permission denied", "/x/agents.json")):
            code, out, err = self.cli("agent", "add", "scout", "--harness", "codex")
        self.assertEqual((code, out), (1, ""))
        self.assertEqual(len(err.strip().splitlines()), 1, err)
        self.assertIn("Load it with: moshi-cockpit agent load scout", err)
        self.assertNotIn("Traceback", err)
        self.assertEqual(self.live_tokens("scout"), [])
        self.assertNotIn(("moshi-scout", "scout"), self.fake.entries)
        self.assert_no_secret(out, err)

    def test_a_file_error_while_loading_drops_the_new_token(self):
        self.moshi.add_agent("writer", harness="codex")
        with mock.patch("cockpit.registry.add", side_effect=OSError(28, "No space left on device")):
            code, _, err = self.cli("agent", "load", "writer")
        self.assertEqual(code, 1)
        self.assertIn("No space left on device", err)
        self.assertEqual(self.live_tokens("writer"), [])

    def test_without_moshi_the_agent_is_local_only(self):
        configure(self.ctx.paths, servers={})
        agent = agents_remote.create(self.ctx, "solo", "codex", api=self.api)
        self.assertEqual(agent.bound, {})
        self.assertEqual(self.moshi.requests, [])
        self.assertEqual([a.name for a in registry.load_agents(self.ctx.paths)], ["solo"])

    # -- load -------------------------------------------------------------------------------------

    def test_load_takes_harness_role_and_skill_from_moshi(self):
        home, _ = self.moshi.add_installation("Home Mac")
        self.moshi.add_agent("Writer", harness="vibe", role="Writes the docs", skill="skill-w", installation_id=home["id"])
        self.moshi.agent("writer")["active_installation_id"] = home["id"]
        agent = agents_remote.load(self.ctx, "writer", api=self.api)
        self.assertEqual((agent.name, agent.harness, agent.role, agent.skill),
                         ("Writer", "vibe", "Writes the docs", "skill-w"))
        token = self.fake.entries[("moshi-Writer", "Writer")]
        self.assertEqual(self.moshi.agent_tokens[token]["installation_id"], self.installation["id"])
        self.assertEqual(self.moshi.agent("writer")["active_installation_id"], home["id"])  # a load takes nothing over
        self.assertEqual(self.fake.entries[("ocellio-Writer", "Writer")], OCELLIO_TOKEN)
        self.assertEqual(registry.find(self.ctx.paths, "writer"), agent)
        self.assert_no_secret()

    def test_load_works_while_the_harness_is_missing_here(self):
        self.moshi.add_agent("writer", harness="vibe", installation_id=None)
        with mock.patch("cockpit.harnesses.base.Harness.installed", return_value=False):
            code, out, err = self.cli("agent", "load", "writer")
        self.assertEqual(code, 0, err)
        self.assertIn("vibe", out)
        self.assertEqual(registry.find(self.ctx.paths, "writer").harness, "vibe")

    def test_load_reports_an_unknown_agent(self):
        code, _, err = self.cli("agent", "load", "ghost")
        self.assertEqual(code, 1)
        self.assertIn("no agent called ghost", err)
        self.assertEqual(registry.load_agents(self.ctx.paths), [])

    def test_an_agent_that_cannot_run_here_is_refused_and_its_new_token_dropped(self):
        self.connected(agents=[{"name": "a1", "harness": "agy"}])
        self.moshi.add_agent("a2", harness="agy", installation_id=None)
        with self.assertRaises(ValueError):
            agents_remote.load(self.ctx, "a2", api=self.api)
        self.assertEqual(self.live_tokens("a2"), [])
        self.assertNotIn(("moshi-a2", "a2"), self.fake.entries)

    def test_load_of_a_name_that_exists_here_never_calls_moshi(self):
        agents_remote.create(self.ctx, "scout", "codex", api=self.api)
        before = len(self.moshi.requests)
        with self.assertRaises(ValueError):
            agents_remote.load(self.ctx, "SCOUT", api=self.api)
        self.assertEqual(len(self.moshi.requests), before)

    # -- catalog ----------------------------------------------------------------------------------

    def test_catalog_lists_local_agents_not_present_here(self):
        home, _ = self.moshi.add_installation("Home Mac")
        agents_remote.create(self.ctx, "here", "codex", api=self.api)
        self.moshi.add_agent("away", harness="vibe", role="Reviews", skill="skill-r", installation_id=home["id"])
        self.moshi.agent("away")["active_installation_id"] = home["id"]
        self.moshi.add_agent("idle", harness="grok", installation_id=None)
        self.moshi.add_agent("hosted", kind="hosted", harness=None)
        with mock.patch("cockpit.harnesses.base.Harness.installed", lambda h, _paths: h.name != "grok"):
            rows = agents_remote.catalog(self.ctx, api=self.api)
        self.assertEqual(rows, [
            {"name": "away", "harness": "vibe", "role": "Reviews", "skill": "skill-r", "active_installation": "Home Mac",
             "harness_installed": True, "loadable": True, "reason": ""},
            {"name": "idle", "harness": "grok", "role": "", "skill": "", "active_installation": None,
             "harness_installed": False, "loadable": False, "reason": "grok cannot run agents of its own"}])
        self.assertEqual(self.moshi.calls("GET", "/api/installation/status")[0]["auth"], f"Bearer {self.device_key}")

    def test_catalog_lists_agents_that_cannot_load_here_with_the_reason(self):
        self.connected(agents=[{"name": "a1", "harness": "agy"}])
        self.moshi.add_agent("a2", harness="agy", installation_id=None)
        self.moshi.add_agent("bare", harness=None, installation_id=None)
        rows = {row["name"]: row for row in agents_remote.catalog(self.ctx, api=self.api)}
        self.assertEqual((rows["a2"]["loadable"], rows["a2"]["reason"]), (False, "one agy agent per Mac, taken by 'a1'"))
        self.assertEqual((rows["bare"]["loadable"], rows["bare"]["reason"]), (False, "no harness in moshi"))

    def test_catalog_json_through_the_cli(self):
        self.moshi.add_agent("away", harness="codex", installation_id=None)
        code, out, err = self.cli("agent", "catalog", "--json")
        self.assertEqual(code, 0, err)
        self.assertEqual([row["name"] for row in json.loads(out)["agents"]], ["away"])
        self.assert_no_secret(out, err)

    def test_catalog_with_a_revoked_device_key_asks_for_approval(self):
        self.moshi.revoke_installation(self.installation["id"])
        self.assertEqual(self.cli("agent", "catalog", "--json")[0], 3)

    # -- set --------------------------------------------------------------------------------------

    def test_set_changes_the_profile_here_and_in_moshi(self):
        agents_remote.create(self.ctx, "scout", "codex", "Research", "skill-a", api=self.api)
        agent, warnings = agents_remote.set_profile(self.ctx, "scout", "Reviews", "", api=self.api)
        self.assertEqual((agent.role, agent.skill, warnings), ("Reviews", "", []))
        self.assertEqual((self.moshi.agent("scout")["role"], self.moshi.agent("scout")["skill"]), ("Reviews", None))
        self.assertEqual(registry.find(self.ctx.paths, "scout").role, "Reviews")

    def test_set_with_a_token_moshi_no_longer_accepts_changes_nothing_here(self):
        agents_remote.create(self.ctx, "scout", "codex", "Research", api=self.api)
        self.moshi.agents["scout"]["is_active"] = False  # moshi answers 401 for its token now
        with self.assertRaises(ValueError) as caught:
            agents_remote.set_profile(self.ctx, "scout", "Reviews", None, api=self.api)
        self.assertIn("no longer accepts", str(caught.exception))
        self.assertEqual(registry.find(self.ctx.paths, "scout").role, "Research")

    def test_a_profile_moshi_refuses_changes_nothing_here(self):
        agents_remote.create(self.ctx, "scout", "codex", "Research", api=self.api)
        with mock.patch("tests.fake_moshi.ROLE_MAX", 3):  # moshi's own check refuses the new role
            with self.assertRaises(ValueError) as caught:
                agents_remote.set_profile(self.ctx, "scout", "Reviews", None, api=self.api)
        self.assertEqual(str(caught.exception), agents_remote.PROFILE)
        self.assertEqual(self.moshi.calls("PATCH", "/api/agent/profile")[0]["body"], {"role": "Reviews"})
        self.assertEqual(registry.find(self.ctx.paths, "scout").role, "Research")
        self.assertEqual(self.moshi.agent("scout")["role"], "Research")

    def test_set_while_moshi_is_unreachable_changes_it_here_with_a_warning(self):
        agents_remote.create(self.ctx, "scout", "codex", "Research", api=self.api)
        agent, warnings = agents_remote.set_profile(self.ctx, "scout", "Reviews", None, api=offline_api())
        self.assertEqual(agent.role, "Reviews")
        self.assertEqual(len(warnings), 1)
        self.assertIn("not reachable", warnings[0])

    def test_the_role_limit_is_moshis(self):
        agent = agents_remote.create(self.ctx, "scout", "codex", "x" * 200, api=self.api)
        self.assertEqual(len(agent.role), registry.MAX_ROLE)
        self.assertEqual(registry.MAX_ROLE, 64)
        self.assertEqual(self.moshi.agent("scout")["role"], "x" * 64)

    def test_the_role_limit_counts_like_moshi(self):
        """UTF-16 units, like moshi's `.length`: an emoji counts two and is never cut in half."""
        self.assertEqual(registry.bound_role("x" * 63 + "😀"), "x" * 63)
        self.assertEqual(registry.bound_role("x" * 62 + "😀😀"), "x" * 62 + "😀")
        agent = agents_remote.create(self.ctx, "scout", "codex", "😀" * 40, api=self.api)
        self.assertEqual(agent.role, "😀" * 32)
        self.assertEqual(self.moshi.agent("scout")["role"], "😀" * 32)  # moshi took it


def offline_api():
    """A MoshiApi aimed at a local port nothing listens on: every call ends as `unreachable`."""
    import socket
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    return MoshiApi(f"http://127.0.0.1:{port}")


if __name__ == "__main__":
    unittest.main()
