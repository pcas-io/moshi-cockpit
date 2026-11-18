"""Moving an earlier setup: import its files during install, adopt its agent tokens after connect."""

import json
import os
import unittest
from unittest import mock
from datetime import datetime, timedelta, timezone

from cockpit import connect, legacy, tokens
from cockpit.agents_remote import ApprovalRequired
from cockpit.backup import BackupStore
from cockpit.config import load_config, origin, setting
from cockpit.moshi_api import MoshiError
from cockpit.registry import load_agents
from tests.helpers import MOSHI, OCELLIO, context, install
from tests.test_agents_remote import OCELLIO_TOKEN, REGISTER_ENTRY, ConnectedHerdr

OLD_REGISTER = "rt_OLD_unscoped_register_0123456789"
OLD_OCELLIO = "pt_OLD_ocellio_cockpit_0123456789"
OLD_OCELLIO_ENTRY = ("ocellio-cockpit", "cockpit")
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
    """An earlier setup as its installer left it: owner-only files written through its own backup store, so
    its list of changes records them as new files (what its uninstall would delete)."""
    data = config if config is not None else {
        "person": "someone", "migrated": True, "servers": {"moshi": MOSHI, "ocellio": OCELLIO},
        "bound": {"cockpit": origin(MOSHI), "admin": origin(MOSHI), "register": origin(MOSHI),
                  "cockpit-ocellio": origin(OCELLIO)}}
    files = {"config.json": json.dumps(data), "agents.json": json.dumps({"agents": list(agents)}), "rules.md": rules}
    store = BackupStore(old_state(paths))
    for name, text in files.items():
        store.write_text(old_dir(paths) / name, text, mode=0o600)
    os.chmod(old_dir(paths), 0o700)
    return store


class ImportTest(unittest.TestCase):
    def setUp(self):
        self.old_entries = {("moshi-scout", "scout"): "bt_LEGACY_scout_0123456789",
                            ("ocellio-scout", "scout"): "pt_LEGACY_scout_0123456789",
                            ("moshi-writer", "writer"): "bt_LEGACY_writer_0123456789",
                            ("ocellio-cockpit", "cockpit"): OLD_OCELLIO, ("moshi-register", "register"): OLD_REGISTER,
                            **HELPER_ENTRIES}
        self.ctx, self.fake = context(self, self.old_entries)
        self.paths = self.ctx.paths

    def run_import(self):
        return legacy.import_config(self.paths, self.ctx.backup, self.ctx.keychain)

    def old_files(self):
        return sorted(f.name for f in old_dir(self.paths).iterdir())

    def test_import_copies_agents_rules_and_servers_and_drops_person_and_old_bindings(self):
        write_old_setup(self.paths)
        notes = self.run_import()
        self.assertEqual(load_config(self.paths).servers, {"moshi": MOSHI, "ocellio": OCELLIO})
        self.assertEqual(load_config(self.paths).rules, "Never touch customer data.")
        self.assertEqual([a.to_json() for a in load_agents(self.paths)], OLD_AGENTS)
        for key in ("person", "migrated"):
            self.assertIsNone(setting(self.paths, key))
        self.assertEqual(setting(self.paths, "bound"), {"ocellio-default": origin(OCELLIO)})
        self.assertNotIn(legacy.FOUND, notes)  # install says it last
        for f in (self.paths.mesh_config, self.paths.agents_file, self.paths.rules_file):
            self.assertEqual(f.stat().st_mode & 0o777, 0o600)

    def test_import_keeps_the_old_config_files_its_backups_would_delete(self):
        write_old_setup(self.paths, agents=OLD_AGENTS + [{"name": "default", "harness": "codex"}])
        self.run_import()
        self.assertEqual(self.old_files(), ["agents.json", "config.json", "rules.md"])
        self.assertIn('"default"', (old_dir(self.paths) / "agents.json").read_text())  # the refused one is not lost

    def test_import_that_stopped_after_the_undo_runs_again_and_carries_everything_over(self):
        settings = self.paths.herdr_settings
        settings.parent.mkdir(parents=True)
        settings.write_text(json.dumps({"theme": "dark"}))
        write_old_setup(self.paths).write_text(settings, json.dumps({"theme": "dark", "old": 1}))
        real = self.ctx.backup.write_text

        def full_disk(path, *args, **kwargs):
            if path == self.paths.mesh_config:
                raise OSError("disk full")
            return real(path, *args, **kwargs)

        self.ctx.backup.write_text = full_disk
        with self.assertRaises(OSError):
            self.run_import()
        self.ctx.backup.write_text = real
        notes = self.run_import()
        self.assertTrue(notes)
        self.assertEqual(load_config(self.paths).servers, {"moshi": MOSHI, "ocellio": OCELLIO})
        self.assertEqual([a.name for a in load_agents(self.paths)], ["scout", "writer"])
        self.assertEqual(load_config(self.paths).rules, "Never touch customer data.")
        self.assertEqual(setting(self.paths, "bound"), {"ocellio-default": origin(OCELLIO)})
        self.assertEqual(self.fake.entries[("ocellio-default", "default")], OLD_OCELLIO)
        self.assertEqual(json.loads(settings.read_text()), {"theme": "dark"})

    def test_import_removes_the_old_registration_token_and_keeps_the_agent_entries(self):
        write_old_setup(self.paths)
        self.run_import()
        self.assertNotIn(REGISTER_ENTRY, self.fake.entries)  # connect stores its own after this
        expected = {k: v for k, v in self.old_entries.items() if k not in (REGISTER_ENTRY, OLD_OCELLIO_ENTRY)}
        expected[("ocellio-default", "default")] = OLD_OCELLIO
        self.assertEqual(self.fake.entries, expected)
        self.assertFalse(self.fake.argv_contains(OLD_REGISTER))

    def test_import_carries_the_ocellio_token_over_so_connect_does_not_ask_for_it(self):
        self.fake.strict[OLD_OCELLIO_ENTRY] = True
        write_old_setup(self.paths)
        self.run_import()
        self.assertNotIn(OLD_OCELLIO_ENTRY, self.fake.entries)
        self.assertTrue(self.ctx.keychain.is_strict("ocellio-default", "default"))
        self.assertEqual(tokens.bound_origin(self.paths, tokens.OCELLIO_DEFAULT), origin(OCELLIO))
        asked = []
        io = connect.ConnectIO(ask_secret=lambda prompt: asked.append(prompt) or "", say=lambda _: None)
        self.assertEqual(connect._ocellio(self.ctx, io, load_config(self.paths)), 0)
        self.assertEqual(asked, [])
        self.assertFalse(self.fake.argv_contains(OLD_OCELLIO))

    def test_import_keeps_an_ocellio_token_bound_to_another_server_out(self):
        write_old_setup(self.paths, config={"servers": {"moshi": MOSHI, "ocellio": OCELLIO},
                                            "bound": {"cockpit-ocellio": "https://elsewhere.example.test"}})
        self.run_import()
        self.assertNotIn(("ocellio-default", "default"), self.fake.entries)
        self.assertIsNone(setting(self.paths, "bound"))
        self.assertIn(OLD_OCELLIO_ENTRY, self.fake.entries)  # migrate removes it with the other helper entries

    def test_a_denied_keychain_read_leaves_the_ocellio_token_for_connect(self):
        from cockpit.secrets import KeychainError
        write_old_setup(self.paths)
        real_read = self.ctx.keychain.read

        def read(service, account):
            if (service, account) == OLD_OCELLIO_ENTRY:
                raise KeychainError("the Keychain refused access")  # the person chose Deny
            return real_read(service, account)

        with mock.patch.object(self.ctx.keychain, "read", side_effect=read):
            notes = self.run_import()
        self.assertEqual(load_config(self.paths).servers, {"moshi": MOSHI, "ocellio": OCELLIO})
        self.assertNotIn(("ocellio-default", "default"), self.fake.entries)
        self.assertIsNone(setting(self.paths, "bound"))
        self.assertIn(OLD_OCELLIO_ENTRY, self.fake.entries)
        self.assertNotIn("ocellio token", notes[-1])
        self.assertTrue((self.paths.state / legacy.IMPORTED_MARKER).exists())

    def test_import_restores_the_old_backups_first(self):
        settings, old_mod = self.paths.herdr_settings, self.paths.herdr_mods / "old-panel" / "mod.js"
        settings.parent.mkdir(parents=True)
        settings.write_text(json.dumps({"theme": "dark"}))
        old_store = write_old_setup(self.paths)
        old_store.write_text(settings, json.dumps({"theme": "dark", "telemetry": False, "old": 1}))
        old_store.write_text(old_mod, "// the earlier panel")
        notes = self.run_import()
        self.assertEqual(json.loads(settings.read_text()), {"theme": "dark"})
        self.assertFalse(old_mod.exists())
        self.assertEqual(old_store.entries(), [])
        self.assertIn("Earlier setup: 2 change(s) undone", notes)

    def test_import_skips_an_old_change_whose_backup_is_missing(self):
        settings, layout = self.paths.herdr_settings, self.paths.herdr_app_home / "layouts" / "cockpit.json"
        for path, text in ((settings, json.dumps({"theme": "dark"})), (layout, "herdr's own layout")):
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(text)
        old_store = write_old_setup(self.paths)
        old_store.write_text(settings, json.dumps({"theme": "dark", "old": 1}))
        old_store.write_text(layout, "the earlier layout")
        os.remove(next(e["backup"] for e in old_store.entries() if e["path"] == str(layout)))
        notes = self.run_import()
        self.assertEqual(layout.read_text(), "the earlier layout")  # never deleted
        self.assertEqual(json.loads(settings.read_text()), {"theme": "dark"})
        self.assertTrue(any("backup is missing" in note and str(layout) in note for note in notes))
        self.assertEqual(old_store.entries(), [])

    def test_import_goes_on_when_the_old_backup_list_is_broken(self):
        write_old_setup(self.paths)
        (old_state(self.paths) / "backups.json").write_text("not json")
        notes = self.run_import()
        self.assertTrue(any("unreadable, nothing undone" in note for note in notes))
        self.assertEqual(load_config(self.paths).servers, {"moshi": MOSHI, "ocellio": OCELLIO})

    def test_install_writes_its_own_backups_over_herdrs_originals(self):
        settings = self.paths.herdr_settings
        settings.parent.mkdir(parents=True)
        settings.write_text(json.dumps({"theme": "dark"}))
        write_old_setup(self.paths).write_text(settings, json.dumps({"theme": "dark", "telemetry": False}))
        notes = install(self.ctx)
        self.assertEqual(notes[-1], legacy.FOUND)
        self.assertEqual(load_config(self.paths).servers, {"moshi": MOSHI, "ocellio": OCELLIO})
        self.assertEqual(json.loads(settings.read_text()), {"theme": "dark", "telemetry": False})
        BackupStore(self.paths.state).restore_all()  # uninstall of the new setup
        self.assertEqual(json.loads(settings.read_text()), {"theme": "dark"})

    def test_import_runs_once_and_only_with_an_earlier_setup(self):
        self.assertEqual(self.run_import(), [])
        self.assertIn(REGISTER_ENTRY, self.fake.entries)
        write_old_setup(self.paths)
        self.assertTrue(self.run_import())
        self.assertEqual(self.run_import(), [])
        self.assertNotIn(legacy.FOUND, install(self.ctx))

    def test_install_after_an_uninstall_does_not_import_again(self):
        write_old_setup(self.paths)
        self.assertEqual(install(self.ctx)[-1], legacy.FOUND)
        BackupStore(self.paths.state).restore_all()  # uninstall
        self.assertFalse(self.paths.mesh_config.exists())
        notes = install(self.ctx)
        self.assertNotIn(legacy.FOUND, notes)
        self.assertEqual(load_agents(self.paths), [])

    def test_import_skips_an_agent_the_new_rules_refuse(self):
        write_old_setup(self.paths, agents=OLD_AGENTS + [{"name": "default", "harness": "codex"}])
        notes = self.run_import()
        self.assertEqual([a.name for a in load_agents(self.paths)], ["scout", "writer"])
        self.assertTrue(any("default" in note and "not carried over" in note for note in notes))

    def test_import_keeps_using_an_agents_folder_of_the_earlier_setup(self):
        vibe_home = legacy.old_agent_home(self.paths, "poet") / "vibe"
        vibe_home.mkdir(parents=True)
        (legacy.old_agent_home(self.paths, "scout")).mkdir(parents=True)
        own = {"name": "own", "harness": "codex", "home": "/somewhere/own"}
        write_old_setup(self.paths, agents=[OLD_AGENTS[0], {"name": "poet", "harness": "vibe"}, own])
        self.run_import()
        homes = {a.name: a.home for a in load_agents(self.paths)}
        self.assertEqual(homes, {"scout": str(legacy.old_agent_home(self.paths, "scout")), "poet": str(vibe_home),
                                 "own": "/somewhere/own"})
        self.assertTrue(vibe_home.is_dir())

    def test_import_refuses_an_old_config_others_can_read_and_writes_nothing(self):
        write_old_setup(self.paths)
        os.chmod(old_dir(self.paths) / "config.json", 0o644)
        with self.assertRaises(ValueError):
            self.run_import()
        self.assertFalse(self.paths.mesh_config.parent.exists())
        self.assertIn(REGISTER_ENTRY, self.fake.entries)
        self.assertEqual(self.old_files(), ["agents.json", "config.json", "rules.md"])


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

    def test_migrate_again_reports_already_on_this_installation(self):
        legacy.migrate(self.ctx, self.api)
        code, out, err = self.cli("migrate")
        self.assertEqual(code, 0, err)
        self.assertIn("scout: already on this installation", out)
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
        self.fake.entries.update({OLD_OCELLIO_ENTRY: OLD_OCELLIO, **HELPER_ENTRIES})
        new_register = self.fake.entries[REGISTER_ENTRY]
        notes = legacy.migrate(self.ctx, self.api)
        for gone in [OLD_OCELLIO_ENTRY, *HELPER_ENTRIES]:
            self.assertNotIn(gone, self.fake.entries)
        self.assertEqual(self.fake.entries[REGISTER_ENTRY], new_register)  # connect's token stays
        self.assertEqual(self.fake.entries[("ocellio-default", "default")], OCELLIO_TOKEN)
        self.assertTrue(any(note.startswith("Removed the earlier helper entries") for note in notes))

    def test_migrate_keeps_the_helper_entries_while_an_agent_is_refused(self):
        self.fake.entries.update(HELPER_ENTRIES)
        self.fake.entries[("moshi-writer", "writer")] = "bt_nobody_knows_this_0123456789"
        legacy.migrate(self.ctx, self.api)
        for kept in HELPER_ENTRIES:
            self.assertIn(kept, self.fake.entries)

    def test_migrate_that_stops_half_way_tells_what_it_moved(self):
        real = self.api

        class GoneAfterOne:
            calls = 0

            def adopt(self, *args):
                GoneAfterOne.calls += 1
                if GoneAfterOne.calls > 1:
                    raise MoshiError("unreachable", 0)
                return real.adopt(*args)

        self.fake.entries.update(HELPER_ENTRIES)
        self.api = GoneAfterOne()
        code, out, err = self.cli("migrate")
        self.assertEqual(code, 1)
        self.assertIn("• scout: moved to this installation", out)
        self.assertIn("Could not reach moshi", err)
        self.assertNotIn(legacy.REVOKE_HINT, out)
        for kept in HELPER_ENTRIES:
            self.assertIn(kept, self.fake.entries)
        self.assert_no_secret(out, err)

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
