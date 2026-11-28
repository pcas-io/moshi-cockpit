"""grok with its own moshi identity: one marked block in ~/.grok/config.toml that names the token variable, written
through the backup store, replaced as a whole, never next to a moshi server of the person's own."""

from __future__ import annotations

import unittest

from cockpit import agents
from cockpit.backup import BackupStore
from cockpit.harnesses.grok import BEGIN, END, FOREIGN_FINDING, moshi_block, with_block, without_block
from cockpit.registry import Agent
from tests.helpers import MOSHI, OWN, configure, context

PERSON = '[ui]\npermission_mode = "default"\n\n[mcp_servers.linear]\nurl = "https://example.test/mcp"\n'


class BlockTest(unittest.TestCase):
    def test_the_block_names_the_variable_never_a_token(self):
        block = moshi_block(MOSHI)
        self.assertIn('headers = { "Authorization" = "${MESH_GROK_MOSHI_AUTH:-}" }', block)
        self.assertNotIn("MESH_MOSHI_TOKEN", block)  # every agent carries that one; its children would inherit it
        self.assertNotIn("Bearer", block)  # no token, no Bearer: an empty header counts nothing at moshi
        self.assertIn(f'url = "{MOSHI}"', block)
        self.assertTrue(block.startswith(BEGIN) and block.rstrip().endswith(END))

    def test_added_once_at_the_end_and_replaced_as_a_whole(self):
        first = with_block(PERSON, MOSHI)
        self.assertTrue(first.startswith(PERSON))
        self.assertEqual(with_block(first, MOSHI), first)  # idempotent
        moved = with_block(first, "https://other.example.test/mcp")
        self.assertEqual(moved.count(BEGIN), 1)
        self.assertNotIn(MOSHI, moved)
        self.assertEqual(without_block(moved).rstrip("\n"), PERSON.rstrip("\n"))

    def test_an_empty_file_gets_only_the_block(self):
        self.assertEqual(with_block("", MOSHI), moshi_block(MOSHI))

    def test_a_moshi_server_of_the_persons_own_is_left_alone(self):
        for own in ('[mcp_servers.moshi]\nurl = "x"\n', '[mcp_servers."moshi"]\n', "[ mcp_servers.moshi.headers ]\n"):
            with self.subTest(own=own), self.assertRaisesRegex(ValueError, "own \\[mcp_servers.moshi\\]"):
                with_block(PERSON + own, MOSHI)
        with_block(PERSON + "[mcp_servers.moshi-dev]\n", MOSHI)  # another name is not ours to judge


class SetupTest(unittest.TestCase):
    def setUp(self):
        self.ctx, _ = context(self, {("moshi-g1", "g1"): OWN})
        configure(self.ctx.paths, servers={"moshi": MOSHI}, agents=[{"name": "g1", "harness": "grok"}])
        self.config = self.ctx.paths.grok_config
        self.config.parent.mkdir(parents=True)
        self.config.write_text(PERSON)

    def test_setup_writes_the_block_and_uninstall_restores_the_file(self):
        changed = agents.setup(self.ctx, Agent(name="g1", harness="grok"))
        self.assertEqual(changed, [str(self.config)])
        self.assertIn(moshi_block(MOSHI), self.config.read_text())
        self.assertNotIn(OWN, self.config.read_text())
        self.assertEqual(self.config.stat().st_mode & 0o777, 0o600)
        self.assertEqual(agents.setup(self.ctx, Agent(name="g1", harness="grok")), [])  # nothing to change
        BackupStore(self.ctx.paths.state).restore_all(keep=self.ctx.paths.mesh_config.parent)
        self.assertEqual(self.config.read_text(), PERSON)

    def test_doctor_reports_a_missing_or_foreign_entry(self):
        self.assertTrue(any("no current moshi entry" in f for f in agents.findings(self.ctx, Agent(name="g1", harness="grok"))))
        self.config.write_text(PERSON + '[mcp_servers.moshi]\nurl = "x"\n')
        self.assertIn(FOREIGN_FINDING, agents.findings(self.ctx, Agent(name="g1", harness="grok")))
        with self.assertRaises(ValueError):
            agents.setup(self.ctx, Agent(name="g1", harness="grok"))
        self.assertTrue(self.config.read_text().endswith('url = "x"\n'))  # untouched

    def test_a_current_entry_is_clean(self):
        agents.setup(self.ctx, Agent(name="g1", harness="grok"))
        found = agents.findings(self.ctx, Agent(name="g1", harness="grok"))
        self.assertFalse(any(".grok/config.toml" in f for f in found), found)


if __name__ == "__main__":
    unittest.main()
