"""`agent remove`: the agent goes from this Mac, and this installation's token in moshi ends with it. The agent
stays in moshi (other installations keep theirs); there is no admin revoke."""

import io
import json
import unittest
from contextlib import redirect_stdout
from unittest import mock

from cockpit import agents_remote, cli, registry, removal, wizard
from cockpit.agents import AgentContext
from cockpit.backup import BackupStore
from cockpit.secrets.macos import MacKeychain
from tests.helpers import FakeSecurity, configure, temp_paths
from tests.test_agents_remote import ConnectedHerdr, offline_api

AGENT_TOKEN, OCELLIO_TOKEN = "bt_AGENT_0123456789", "pt_OCELLIO_0123456789"


class RemoveFromMoshiTest(ConnectedHerdr, unittest.TestCase):
    def gone(self, name):
        self.assertEqual(registry.load_agents(self.ctx.paths), [])
        self.assertNotIn((f"moshi-{name}", name), self.fake.entries)
        self.assertNotIn((f"ocellio-{name}", name), self.fake.entries)

    def test_remove_drops_this_installations_token_only(self):
        agents_remote.create(self.ctx, "scout", "codex", api=self.api)
        home, _ = self.moshi.add_installation("Home Mac")
        elsewhere = self.moshi.issue_agent_token("scout", home["id"])
        here = self.fake.entries[("moshi-scout", "scout")]
        notes, warnings = removal.remove_agent(self.ctx, "scout", api=self.api)
        self.assertEqual(warnings, [])
        self.assertEqual(self.moshi.calls("DELETE", "/api/agent/token")[0]["auth"], f"Bearer {here}")
        self.assertTrue(self.moshi.agent_tokens[here]["revoked"])
        self.assertEqual(self.live_tokens("scout"), [elsewhere])  # the other installation keeps its token
        self.assertIsNotNone(self.moshi.agent("scout"))           # the agent stays in moshi
        self.assertIsNone(self.moshi.agent("scout")["active_installation_id"])  # it was held here
        self.gone("scout")
        self.assert_no_secret(" ".join(notes))

    def test_remove_works_offline_with_a_warning(self):
        agents_remote.create(self.ctx, "scout", "codex", api=self.api)
        notes, warnings = removal.remove_agent(self.ctx, "scout", api=offline_api())
        self.assertEqual(len(warnings), 1)
        self.assertIn("not reachable", warnings[0])
        self.gone("scout")

    def test_remove_with_a_token_moshi_no_longer_accepts_warns_once(self):
        agents_remote.create(self.ctx, "scout", "codex", api=self.api)
        self.moshi.agent_tokens[self.fake.entries[("moshi-scout", "scout")]]["revoked"] = True
        _, warnings = removal.remove_agent(self.ctx, "scout", api=self.api)
        self.assertEqual(len(warnings), 1)
        self.assertIn("no longer accepts", warnings[0])
        self.gone("scout")

    def test_cli_remove_with_the_panels_flag_prints_the_warning_and_succeeds(self):
        agents_remote.create(self.ctx, "scout", "codex", api=self.api)
        self.api = offline_api()
        code, out, err = self.cli("agent", "remove", "scout", "--keep-moshi")
        self.assertEqual(code, 0)
        self.assertIn("scout removed", out)
        self.assertEqual(err.count("Warning:"), 1)
        self.gone("scout")
        self.assert_no_secret(out, err)


class RemoveLocallyTest(unittest.TestCase):
    def ctx(self, servers=None, harness="codex"):
        paths = temp_paths(self)
        configure(paths, servers=servers, agents=[{"name": "research", "harness": harness}])
        fake = FakeSecurity({("moshi-research", "research"): AGENT_TOKEN, ("ocellio-research", "research"): OCELLIO_TOKEN,
                             ("moshi-admin", "admin"): "admin_TOKEN_0123456789"})
        return AgentContext(paths=paths, backup=BackupStore(paths.state), keychain=MacKeychain(fake)), fake

    def test_no_moshi_means_no_call_and_no_warning(self):
        ctx, fake = self.ctx(servers={})
        api = mock.Mock()
        notes, warnings = removal.remove_agent(ctx, "research", api=api)
        api.drop_token.assert_not_called()
        self.assertEqual(warnings, [])
        self.assertEqual(registry.load_agents(ctx.paths), [])
        self.assertNotIn(("moshi-research", "research"), fake.entries)
        self.assertIn(("moshi-admin", "admin"), fake.entries)  # never read, never used, never touched
        self.assertFalse(any("-w" in args for args, _ in fake.calls))  # no token is read at all
        for secret in (AGENT_TOKEN, OCELLIO_TOKEN):
            self.assertNotIn(secret, " ".join(notes))

    def test_an_agent_without_its_own_token_never_calls_moshi(self):
        ctx, fake = self.ctx(harness="claude")
        del fake.entries[("moshi-research", "research")]
        api = mock.Mock()
        _, warnings = removal.remove_agent(ctx, "research", api=api)
        api.drop_token.assert_not_called()
        self.assertEqual((warnings, registry.load_agents(ctx.paths)), ([], []))

    def test_a_token_bound_to_another_server_is_never_sent(self):
        ctx, fake = self.ctx()
        data = json.loads(ctx.paths.agents_file.read_text())
        data["agents"][0]["bound"]["moshi"] = "https://other.example.test"
        ctx.paths.agents_file.write_text(json.dumps(data))
        api = mock.Mock()
        _, warnings = removal.remove_agent(ctx, "research", api=api)
        api.drop_token.assert_not_called()
        self.assertEqual(len(warnings), 1)
        self.assertEqual(registry.load_agents(ctx.paths), [])

    def test_status_json_has_no_admin_field(self):
        ctx, fake = self.ctx(servers={})
        with mock.patch("cockpit.cli.Paths.from_env", return_value=ctx.paths), \
                mock.patch("cockpit.cli.default_store", return_value=ctx.keychain), redirect_stdout(io.StringIO()) as out:
            self.assertEqual(cli.main(["status", "--json"]), 0)
            self.assertEqual(cli.main(["agent", "list", "--json"]), 0)
        for line in out.getvalue().splitlines():
            self.assertNotIn("admin", json.loads(line))


class LocalAddTest(unittest.TestCase):
    def test_add_without_moshi_says_it_is_local_only(self):
        paths = temp_paths(self)
        configure(paths, servers={})
        ctx = AgentContext(paths=paths, backup=BackupStore(paths.state), keychain=MacKeychain(FakeSecurity()))
        notes = wizard.create(ctx, "solo", "codex")
        self.assertTrue(any("Local only" in n for n in notes))  # wizard.py's own wording


if __name__ == "__main__":
    unittest.main()
