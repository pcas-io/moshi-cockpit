"""`agent remove`: the agent goes from this Mac; its moshi identity stays and the output says where to revoke it."""

import io
import json
import unittest
from contextlib import redirect_stdout
from unittest import mock

from cockpit import cli, registry, removal, wizard
from cockpit.agents import AgentContext
from cockpit.backup import BackupStore
from cockpit.secrets.macos import MacKeychain
from tests.helpers import FakeSecurity, configure, temp_paths

AGENT_TOKEN, OCELLIO_TOKEN = "bt_AGENT_0123456789", "pt_OCELLIO_0123456789"


class RemovalTest(unittest.TestCase):
    def ctx(self, servers=None, harness="codex"):
        paths = temp_paths(self)
        configure(paths, servers=servers, agents=[{"name": "research", "harness": harness}])
        fake = FakeSecurity({("moshi-research", "research"): AGENT_TOKEN, ("ocellio-research", "research"): OCELLIO_TOKEN,
                             ("moshi-admin", "admin"): "admin_TOKEN_0123456789"})
        return AgentContext(paths=paths, backup=BackupStore(paths.state), keychain=MacKeychain(fake)), fake

    def gone(self, ctx, fake):
        self.assertEqual(registry.load_agents(ctx.paths), [])
        self.assertNotIn(("moshi-research", "research"), fake.entries)
        self.assertNotIn(("ocellio-research", "research"), fake.entries)

    def no_secret(self, notes, fake):
        text = " ".join(notes)
        for secret in (AGENT_TOKEN, OCELLIO_TOKEN):
            self.assertNotIn(secret, text)
            self.assertFalse(fake.argv_contains(secret))

    def test_the_agent_goes_and_its_moshi_identity_stays_with_a_hint(self):
        ctx, fake = self.ctx()
        notes = removal.remove_agent(ctx, "research")
        self.assertTrue(any("stays active" in n and "dashboard" in n for n in notes), notes)
        self.gone(ctx, fake)
        self.no_secret(notes, fake)
        self.assertIn(("moshi-admin", "admin"), fake.entries)  # never read, never used, never touched
        self.assertFalse(any("-w" in args for args, _ in fake.calls))  # no token is read at all

    def test_an_agent_without_its_own_token_says_nothing_about_moshi(self):
        ctx, fake = self.ctx(harness="claude")
        del fake.entries[("moshi-research", "research")]
        notes = removal.remove_agent(ctx, "research")
        self.assertFalse(any("moshi identity" in n for n in notes))
        self.assertEqual(registry.load_agents(ctx.paths), [])

    def test_no_moshi_means_no_moshi_note(self):
        ctx, fake = self.ctx(servers={})
        notes = removal.remove_agent(ctx, "research")
        self.assertFalse(any("moshi identity" in n for n in notes))
        self.gone(ctx, fake)

    def test_cli_remove_with_the_panels_flag(self):
        for argv in (["agent", "remove", "research"], ["agent", "remove", "research", "--keep-moshi"]):
            ctx, fake = self.ctx()
            with self.subTest(argv=argv), mock.patch("cockpit.cli.Paths.from_env", return_value=ctx.paths), \
                    mock.patch("cockpit.cli.default_store", return_value=ctx.keychain), \
                    redirect_stdout(io.StringIO()) as out:
                self.assertEqual(cli.main(argv), 0)
                self.assertIn("stays active", out.getvalue())
                self.assertNotIn(AGENT_TOKEN, out.getvalue())
                self.gone(ctx, fake)

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
        self.assertTrue(any("Nur lokal" in n for n in notes))  # wizard.py's own wording


if __name__ == "__main__":
    unittest.main()
