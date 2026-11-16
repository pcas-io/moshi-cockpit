"""`agent remove` reaches moshi (found in an audit: removing an agent left its moshi identity behind)."""

import io
import json
import unittest
from contextlib import redirect_stdout
from unittest import mock

from cockpit import admin, cli, registry, removal, wizard
from cockpit.agents import AgentContext
from cockpit.backup import BackupStore
from cockpit.keychain import Keychain
from tests.helpers import FakeSecurity, configure, temp_paths

AGENT_TOKEN, OCELLIO_TOKEN, ADMIN_TOKEN = "bt_AGENT_0123456789", "pt_OCELLIO_0123456789", "admin_TOKEN_0123456789"


class FakeAdmin:
    def __init__(self, error=None):
        self.revoked, self.error = [], error

    def revoke_agent(self, name):
        if self.error:
            raise self.error
        self.revoked.append(name)
        return {"agent": {"name": name}, "is_active": False}


class RemovalTest(unittest.TestCase):
    def ctx(self, admin_token=True, servers=None, harness="codex"):
        paths = temp_paths(self)
        configure(paths, servers=servers, person="anna", agents=[{"name": "research", "harness": harness}])
        entries = {("moshi-research", "research"): AGENT_TOKEN, ("ocellio-research", "research"): OCELLIO_TOKEN}
        if admin_token:
            entries[(admin.ADMIN_SERVICE, admin.ADMIN_ACCOUNT)] = ADMIN_TOKEN
        fake = FakeSecurity(entries)
        return AgentContext(paths=paths, backup=BackupStore(paths.state), keychain=Keychain(fake)), fake

    def gone(self, ctx, fake):
        self.assertEqual(registry.load_agents(ctx.paths), [])
        self.assertNotIn(("moshi-research", "research"), fake.entries)
        self.assertNotIn(("ocellio-research", "research"), fake.entries)

    def no_secret(self, notes, fake):
        text = " ".join(notes)
        for secret in (AGENT_TOKEN, OCELLIO_TOKEN, ADMIN_TOKEN):
            self.assertNotIn(secret, text)
            self.assertFalse(fake.argv_contains(secret))

    def test_with_admin_access_it_asks_then_revokes_by_moshi_name(self):
        ctx, fake = self.ctx()
        api, asked = FakeAdmin(), []
        notes = removal.remove_agent(ctx, "research", confirm=lambda q: asked.append(q) or True, admin_api=lambda: api)
        self.assertEqual(api.revoked, ["anna-research"])  # the person's moshi name
        self.assertIn("anna-research", asked[0])
        self.assertTrue(any("widerrufen" in n for n in notes))
        self.gone(ctx, fake)
        self.no_secret(notes, fake)

    def test_declined_or_without_admin_access_the_identity_stays_and_it_says_so(self):
        for admin_token, confirm in ((True, lambda _q: False), (False, lambda _q: self.fail("nothing to ask"))):
            with self.subTest(admin_token=admin_token):
                ctx, fake = self.ctx(admin_token=admin_token)
                notes = removal.remove_agent(ctx, "research", confirm=confirm,
                                             admin_api=lambda: self.fail("no revoke"))
                self.assertTrue(any("bleibt aktiv" in n and "Dashboard" in n for n in notes))
                self.gone(ctx, fake)
                self.no_secret(notes, fake)

    def test_a_failed_revoke_touches_nothing_local(self):
        ctx, fake = self.ctx()
        api = FakeAdmin(error=admin.AdminError("moshi nicht erreichbar"))
        with self.assertRaises(admin.AdminError) as caught:
            removal.remove_agent(ctx, "research", revoke=True, admin_api=lambda: api)
        self.assertIn("Nichts entfernt", str(caught.exception))
        self.assertEqual([a.name for a in registry.load_agents(ctx.paths)], ["research"])
        self.assertIn(("moshi-research", "research"), fake.entries)

    def test_an_identity_moshi_no_longer_knows_does_not_block(self):
        ctx, fake = self.ctx()
        api = FakeAdmin(error=admin.AdminError("not found", reason="not_found", status=404))
        notes = removal.remove_agent(ctx, "research", revoke=True, admin_api=lambda: api)
        self.assertTrue(any("gibt es in moshi nicht" in n for n in notes))
        self.gone(ctx, fake)

    def test_a_404_that_is_not_moshis_own_answer_removes_nothing(self):
        ctx, fake = self.ctx()
        api = FakeAdmin(error=admin.AdminError("moshi kennt das nicht", reason="", status=404))
        with self.assertRaises(admin.AdminError) as caught:
            removal.remove_agent(ctx, "research", revoke=True, admin_api=lambda: api)
        self.assertIn("Nichts entfernt", str(caught.exception))
        self.assertEqual([a.name for a in registry.load_agents(ctx.paths)], ["research"])
        self.assertIn(("moshi-research", "research"), fake.entries)

    def test_an_agent_without_its_own_token_never_revokes_the_name(self):
        # The name may belong to someone else in the mesh (a session elsewhere with the same name).
        ctx, fake = self.ctx(harness="claude")
        del fake.entries[("moshi-research", "research")]
        notes = removal.remove_agent(ctx, "research", revoke=True, admin_api=lambda: self.fail("no revoke"))
        self.assertTrue(any("nicht angefasst" in n for n in notes))
        self.assertEqual(registry.load_agents(ctx.paths), [])

    def test_no_moshi_means_no_revoke(self):
        ctx, fake = self.ctx(servers={}, harness="codex")
        notes = removal.remove_agent(ctx, "research", revoke=True, admin_api=lambda: self.fail("no revoke"))
        self.assertFalse(any("moshi-Identität" in n for n in notes))
        self.assertEqual(registry.load_agents(ctx.paths), [])

    def test_a_claude_agent_has_its_own_identity_to_revoke(self):
        ctx, fake = self.ctx(harness="claude")
        api = FakeAdmin()
        removal.remove_agent(ctx, "research", revoke=True, admin_api=lambda: api)
        self.assertEqual(api.revoked, ["anna-research"])

    def test_cli_without_terminal_never_revokes_unasked(self):
        ctx, fake = self.ctx()
        api = FakeAdmin()
        with mock.patch("cockpit.cli.Paths.from_env", return_value=ctx.paths), \
                mock.patch("cockpit.cli.Keychain", return_value=ctx.keychain), \
                mock.patch("cockpit.removal.AdminApi.from_keychain", return_value=api), \
                mock.patch("sys.stdin.isatty", return_value=False), redirect_stdout(io.StringIO()) as out:
            self.assertEqual(cli.main(["agent", "remove", "research"]), 0)
        self.assertEqual(api.revoked, [])
        self.assertIn("bleibt aktiv", out.getvalue())
        self.assertNotIn(AGENT_TOKEN, out.getvalue())

    def test_cli_revoke_flag_is_the_panels_answer(self):
        ctx, fake = self.ctx()
        api = FakeAdmin()
        with mock.patch("cockpit.cli.Paths.from_env", return_value=ctx.paths), \
                mock.patch("cockpit.cli.Keychain", return_value=ctx.keychain), \
                mock.patch("cockpit.removal.AdminApi.from_keychain", return_value=api), \
                mock.patch("sys.stdin.isatty", return_value=False), redirect_stdout(io.StringIO()) as out:
            self.assertEqual(cli.main(["agent", "remove", "research", "--revoke-moshi"]), 0)
        self.assertEqual(api.revoked, ["anna-research"])
        self.assertNotIn(ADMIN_TOKEN, out.getvalue())

    def test_revoke_name_cannot_leave_the_url_path(self):
        seen = []
        api = admin.AdminApi(ADMIN_TOKEN, "https://moshi.example.test/mcp", opener=lambda r, timeout: seen.append(r))
        for bad in ("../board", "a/b", "", "-x"):
            with self.assertRaises(admin.AdminError):
                api.revoke_agent(bad)
        self.assertEqual(seen, [])

    def test_status_json_says_whether_admin_access_is_there_without_reading_it(self):
        ctx, fake = self.ctx(servers={})
        with mock.patch("cockpit.cli.Paths.from_env", return_value=ctx.paths), \
                mock.patch("cockpit.cli.Keychain", return_value=ctx.keychain), redirect_stdout(io.StringIO()) as out:
            self.assertEqual(cli.main(["status", "--json"]), 0)
        self.assertIs(json.loads(out.getvalue())["admin"], True)
        self.assertFalse(any("-w" in args for args, _ in fake.calls))


class LocalAddTest(unittest.TestCase):
    def test_add_without_moshi_says_it_is_local_only(self):
        paths = temp_paths(self)
        configure(paths, servers={})
        ctx = AgentContext(paths=paths, backup=BackupStore(paths.state), keychain=Keychain(FakeSecurity()))
        notes = wizard.create(ctx, "solo", "codex")
        self.assertTrue(any("Nur lokal" in n for n in notes))


if __name__ == "__main__":
    unittest.main()
