"""Moving an earlier setup: import its files during install, adopt its agent tokens after connect."""

import json
import os
import unittest
from datetime import datetime, timedelta, timezone

from cockpit import legacy
from cockpit.agents_remote import ApprovalRequired
from cockpit.backup import BackupStore
from cockpit.config import load_config, origin, setting
from cockpit.registry import load_agents
from tests.helpers import MOSHI, OCELLIO, context, install
from tests.test_agents_remote import OCELLIO_TOKEN, REGISTER_ENTRY, ConnectedHerdr

OLD_REGISTER = "rt_OLD_unscoped_register_0123456789"
OLD_OCELLIO = "pt_OLD_ocellio_cockpit_0123456789"
HELPER_ENTRIES = {("moshi-cockpit", "cockpit"): "bt_OLD_cockpit_identity_0123456789",
                  ("moshi-admin", "admin"): "mesh_admin_OLD_0123456789"}
OLD_AGENTS = [{"name": "scout", "harness": "codex", "role": "Research",
               "bound": {"moshi": origin(MOSHI), "ocellio": origin(OCELLIO)}},
              {"name": "writer", "harness": "claude", "skill": "skill-a", "bound": {"moshi": origin(MOSHI)}}]


def old_dir(paths):
    return paths.config / "mesh-cockpit"


def old_state(paths):
    return paths.home / ".local" / "state" / "mesh-cockpit"


def write_old_setup(paths, config=None, agents=OLD_AGENTS, rules="Never touch customer data."):
    """An earlier setup as its installer left it: owner-only files in an owner-only folder."""
    folder = old_dir(paths)
    folder.mkdir(mode=0o700, parents=True)
    data = config if config is not None else {
        "person": "someone", "migrated": True, "servers": {"moshi": MOSHI, "ocellio": OCELLIO},
        "bound": {"cockpit": origin(MOSHI), "admin": origin(MOSHI), "register": origin(MOSHI),
                  "cockpit-ocellio": origin(OCELLIO)}}
    files = {"config.json": json.dumps(data), "agents.json": json.dumps({"agents": list(agents)}), "rules.md": rules}
    for name, text in files.items():
        (folder / name).write_text(text)
        os.chmod(folder / name, 0o600)


class ImportTest(unittest.TestCase):
    def setUp(self):
        self.old_entries = {("moshi-scout", "scout"): "bt_LEGACY_scout_0123456789",
                            ("ocellio-scout", "scout"): "pt_LEGACY_scout_0123456789",
                            ("moshi-writer", "writer"): "bt_LEGACY_writer_0123456789",
                            ("ocellio-cockpit", "cockpit"): OLD_OCELLIO, ("moshi-register", "register"): OLD_REGISTER,
                            **HELPER_ENTRIES}
        self.ctx, self.fake = context(self, self.old_entries)
        self.paths = self.ctx.paths

    def test_import_copies_agents_rules_and_servers_and_drops_person_and_old_bindings(self):
        write_old_setup(self.paths)
        notes = legacy.import_config(self.paths, self.ctx.backup, self.ctx.keychain)
        self.assertEqual(load_config(self.paths).servers, {"moshi": MOSHI, "ocellio": OCELLIO})
        self.assertEqual(load_config(self.paths).rules, "Never touch customer data.")
        self.assertEqual([a.to_json() for a in load_agents(self.paths)], OLD_AGENTS)
        for key in ("person", "migrated"):
            self.assertIsNone(setting(self.paths, key))
        self.assertEqual(setting(self.paths, "bound"), {legacy.OLD_OCELLIO_BINDING: origin(OCELLIO)})
        self.assertNotIn(legacy.FOUND, notes)  # install says it last
        for f in (self.paths.mesh_config, self.paths.agents_file, self.paths.rules_file):
            self.assertEqual(f.stat().st_mode & 0o777, 0o600)

    def test_import_removes_the_old_registration_token_and_keeps_the_agent_entries(self):
        write_old_setup(self.paths)
        legacy.import_config(self.paths, self.ctx.backup, self.ctx.keychain)
        self.assertNotIn(REGISTER_ENTRY, self.fake.entries)  # connect stores its own after this
        expected = {k: v for k, v in self.old_entries.items() if k != REGISTER_ENTRY}
        self.assertEqual(self.fake.entries, expected)
        self.assertFalse(self.fake.argv_contains(OLD_REGISTER))

    def test_import_restores_the_old_backups_first(self):
        settings, old_mod = self.paths.herdr_settings, self.paths.herdr_mods / "old-panel" / "mod.js"
        settings.parent.mkdir(parents=True)
        settings.write_text(json.dumps({"theme": "dark"}))
        old_store = BackupStore(old_state(self.paths))
        old_store.write_text(settings, json.dumps({"theme": "dark", "telemetry": False, "old": 1}))
        old_store.write_text(old_mod, "// the earlier panel")
        write_old_setup(self.paths)
        notes = legacy.import_config(self.paths, self.ctx.backup, self.ctx.keychain)
        self.assertEqual(json.loads(settings.read_text()), {"theme": "dark"})
        self.assertFalse(old_mod.exists())
        self.assertEqual(old_store.entries(), [])
        self.assertTrue(any("restored" in note for note in notes))

    def test_import_goes_on_when_the_old_backup_list_is_broken(self):
        old_state(self.paths).mkdir(parents=True)
        (old_state(self.paths) / "backups.json").write_text("not json")
        write_old_setup(self.paths)
        notes = legacy.import_config(self.paths, self.ctx.backup, self.ctx.keychain)
        self.assertTrue(any("not every change could be restored" in note for note in notes))
        self.assertEqual(load_config(self.paths).servers, {"moshi": MOSHI, "ocellio": OCELLIO})

    def test_install_writes_its_own_backups_over_herdrs_originals(self):
        settings = self.paths.herdr_settings
        settings.parent.mkdir(parents=True)
        settings.write_text(json.dumps({"theme": "dark"}))
        BackupStore(old_state(self.paths)).write_text(settings, json.dumps({"theme": "dark", "telemetry": False}))
        write_old_setup(self.paths)
        notes = install(self.ctx)
        self.assertEqual(notes[-1], legacy.FOUND)
        self.assertEqual(load_config(self.paths).servers, {"moshi": MOSHI, "ocellio": OCELLIO})
        self.assertEqual(json.loads(settings.read_text()), {"theme": "dark", "telemetry": False})
        BackupStore(self.paths.state).restore_all()  # uninstall of the new setup
        self.assertEqual(json.loads(settings.read_text()), {"theme": "dark"})

    def test_import_runs_once_and_only_with_an_earlier_setup(self):
        self.assertEqual(legacy.import_config(self.paths, self.ctx.backup, self.ctx.keychain), [])
        self.assertIn(REGISTER_ENTRY, self.fake.entries)
        write_old_setup(self.paths)
        self.assertTrue(legacy.import_config(self.paths, self.ctx.backup, self.ctx.keychain))
        self.assertEqual(legacy.import_config(self.paths, self.ctx.backup, self.ctx.keychain), [])
        self.assertNotIn(legacy.FOUND, install(self.ctx))

    def test_import_skips_an_agent_the_new_rules_refuse(self):
        write_old_setup(self.paths, agents=OLD_AGENTS + [{"name": "default", "harness": "codex"}])
        notes = legacy.import_config(self.paths, self.ctx.backup, self.ctx.keychain)
        self.assertEqual([a.name for a in load_agents(self.paths)], ["scout", "writer"])
        self.assertTrue(any("default" in note and "not carried over" in note for note in notes))

    def test_import_refuses_an_old_config_others_can_read_and_writes_nothing(self):
        write_old_setup(self.paths)
        os.chmod(old_dir(self.paths) / "config.json", 0o644)
        with self.assertRaises(ValueError):
            legacy.import_config(self.paths, self.ctx.backup, self.ctx.keychain)
        self.assertFalse(self.paths.mesh_config.parent.exists())
        self.assertIn(REGISTER_ENTRY, self.fake.entries)


class MigrateTest(ConnectedHerdr, unittest.TestCase):
    def setUp(self):
        super().setUp()
        self.legacy = {name: self.moshi.add_agent(name, kind="hosted", harness=None) for name in ("scout", "writer")}
        self.connected(agents=OLD_AGENTS)
        self.fake.entries.update({("moshi-scout", "scout"): self.legacy["scout"],
                                  ("moshi-writer", "writer"): self.legacy["writer"]})

    def test_migrate_adopts_legacy_tokens_and_keeps_the_keychain_entries(self):
        notes = legacy.migrate(self.ctx, self.api)
        for name, token in self.legacy.items():
            self.assertEqual(self.moshi.agent(name)["active_installation_id"], self.installation["id"])
            self.assertEqual(self.fake.entries[(f"moshi-{name}", name)], token)
            self.assertEqual(self.api.activate(token), "Work Mac")  # the same token is this installation's now
        self.assertEqual(self.moshi.agent("writer")["harness"], "claude")
        self.assertIn("scout: moved to this installation", notes)
        self.assertEqual(notes[-1], legacy.REVOKE_HINT)
        self.assert_no_secret("\n".join(notes))

    def test_migrate_again_reports_already_moved(self):
        legacy.migrate(self.ctx, self.api)
        code, out, err = self.cli("migrate")
        self.assertEqual(code, 0, err)
        self.assertIn("scout: already moved", out)
        self.assertIn(legacy.REVOKE_HINT, out)
        self.assert_no_secret(out, err)

    def test_migrate_needs_a_valid_approval(self):
        self.expires(datetime.now(timezone.utc) - timedelta(minutes=1))
        with self.assertRaises(ApprovalRequired):
            legacy.migrate(self.ctx, self.api)
        code, _, _ = self.cli("migrate")
        self.assertEqual(code, 3)
        self.assertEqual(self.moshi.calls("POST", "/api/register/agents/adopt"), [])

    def test_migrate_exits_3_when_moshi_refuses_the_registration_token(self):
        self.fake.entries.update(HELPER_ENTRIES)
        self.moshi.add_registration_token(self.installation["id"])  # revokes the stored one
        code, _, _ = self.cli("migrate")
        self.assertEqual(code, 3)
        for entry in HELPER_ENTRIES:
            self.assertIn(entry, self.fake.entries)  # nothing cleaned up before the move worked

    def test_migrate_reports_a_refused_agent_and_moves_the_others(self):
        self.fake.entries[("moshi-writer", "writer")] = "bt_nobody_knows_this_0123456789"
        notes = legacy.migrate(self.ctx, self.api)
        self.assertIn("scout: moved to this installation", notes)
        self.assertTrue(any(note.startswith("writer: not moved") for note in notes))

    def test_migrate_sends_no_token_bound_to_another_server(self):
        agents = [dict(OLD_AGENTS[0], bound={"moshi": "https://elsewhere.example.test"}), OLD_AGENTS[1]]
        self.connected(agents=agents)
        notes = legacy.migrate(self.ctx, self.api)
        bodies = [call["body"]["token"] for call in self.moshi.calls("POST", "/api/register/agents/adopt")]
        self.assertEqual(bodies, [self.legacy["writer"]])
        self.assertTrue(any(note.startswith("scout: not moved") for note in notes))

    def test_migrate_removes_the_old_helper_entries(self):
        del self.fake.entries[("ocellio-default", "default")]
        self.fake.entries.update({("ocellio-cockpit", "cockpit"): OLD_OCELLIO, **HELPER_ENTRIES})
        data = json.loads(self.ctx.paths.mesh_config.read_text())
        data["bound"].pop("ocellio-default")
        data["bound"][legacy.OLD_OCELLIO_BINDING] = origin(OCELLIO)
        self.ctx.paths.mesh_config.write_text(json.dumps(data))
        new_register = self.fake.entries[REGISTER_ENTRY]
        legacy.migrate(self.ctx, self.api)
        self.assertEqual(self.fake.entries[("ocellio-default", "default")], OLD_OCELLIO)
        self.assertEqual(setting(self.ctx.paths, "bound").get("ocellio-default"), origin(OCELLIO))
        self.assertNotIn(legacy.OLD_OCELLIO_BINDING, setting(self.ctx.paths, "bound"))
        for gone in [("ocellio-cockpit", "cockpit"), *HELPER_ENTRIES]:
            self.assertNotIn(gone, self.fake.entries)
        self.assertEqual(self.fake.entries[REGISTER_ENTRY], new_register)  # connect's token stays

    def test_migrate_keeps_an_ocellio_token_connect_stored(self):
        self.fake.entries[("ocellio-cockpit", "cockpit")] = OLD_OCELLIO
        legacy.migrate(self.ctx, self.api)
        self.assertEqual(self.fake.entries[("ocellio-default", "default")], OCELLIO_TOKEN)
        self.assertNotIn(("ocellio-cockpit", "cockpit"), self.fake.entries)

    def test_install_removes_the_old_registration_token_before_connect_and_migrate_keeps_the_new_one(self):
        ctx, fake = context(self, {REGISTER_ENTRY: OLD_REGISTER, **HELPER_ENTRIES})
        write_old_setup(ctx.paths)
        install(ctx)
        self.assertNotIn(REGISTER_ENTRY, fake.entries)
        # connect: a fresh registration token, bound, with its expiry
        fake.entries.update({REGISTER_ENTRY: self.rt, ("moshi-device", "device"): self.device_key,
                             ("ocellio-default", "default"): OCELLIO_TOKEN})
        self.ctx, self.fake = ctx, fake
        self.connected()
        legacy.migrate(ctx, self.api)
        self.assertEqual(fake.entries[REGISTER_ENTRY], self.rt)
        for gone in HELPER_ENTRIES:
            self.assertNotIn(gone, fake.entries)


if __name__ == "__main__":
    unittest.main()
