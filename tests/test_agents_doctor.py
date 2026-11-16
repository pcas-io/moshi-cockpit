import json
import os
import stat
import tempfile
import unittest
from unittest import mock
from pathlib import Path

from cockpit import agents, bots, defaults, doctor, installer, registry
from cockpit.agents import AgentContext
from cockpit.backup import BackupStore
from cockpit.keychain import Keychain
from cockpit.pins import PinError, load_pins
from tests.helpers import MOSHI, OCELLIO, FakeSecurity, configure, temp_paths

OC, MO = "oc_TOKEN_aaaaaaaa", "bt_TOKEN_bbbbbbbb"


def context(test, entries=None):
    paths = temp_paths(test)
    fake = FakeSecurity(entries or {})
    return AgentContext(paths=paths, backup=BackupStore(paths.state), keychain=Keychain(fake)), fake


VIBE = {"name": "vibe", "harness": "vibe"}
AGY = {"name": "agy", "harness": "agy"}
CODEX = {"name": "codex", "harness": "codex"}


def registered(test, entries=None, servers=None, agents_=(VIBE, AGY, CODEX), person=None):
    # The CLIs themselves are not installed in CI; whether they are is not what these tests check.
    patcher = mock.patch("cockpit.harnesses.base.Harness.installed", return_value=True)
    patcher.start()
    test.addCleanup(patcher.stop)
    ctx, fake = context(test, entries)
    configure(ctx.paths, servers=servers, agents=agents_, person=person)
    agents.link_launchers(ctx)
    return ctx, fake


def agent(ctx, name):
    return registry.find(ctx.paths, name)


class AgentSetupTest(unittest.TestCase):
    def test_vibe_profile_per_agent_from_the_configured_servers(self):
        ctx, _ = registered(self, {("moshi-research", "research"): MO, ("ocellio-research", "research"): OC},
                            agents_=[{"name": "research", "harness": "vibe", "role": "Recherche"}])
        agents.setup(ctx, agent(ctx, "research"))
        home = ctx.paths.agent_home("research") / "vibe"
        config = (home / "config.toml").read_text()
        self.assertIn("enable_connectors = false", config)
        self.assertIn(OCELLIO, config)
        self.assertIn(MOSHI, config)
        self.assertNotIn(OC, config)
        self.assertIn('[tools.ocellio_update_entity]\npermission = "ask"', config)
        presence = (home / "AGENTS.md").read_text()
        self.assertIn("`research`", presence)
        self.assertIn("Rolle: Recherche", presence)
        self.assertEqual(agents.findings(ctx, agent(ctx, "research")), [])

    def test_without_servers_no_tokens_and_no_mesh_wiring(self):
        ctx, fake = registered(self, servers={}, agents_=[{"name": "solo", "harness": "vibe"}])
        agents.setup(ctx, agent(ctx, "solo"))
        config = (ctx.paths.agent_home("solo") / "vibe" / "config.toml").read_text()
        self.assertNotIn("[[mcp_servers]]", config)
        self.assertNotIn("moshi", (ctx.paths.agent_home("solo") / "vibe" / "AGENTS.md").read_text())
        self.assertEqual(fake.entries, {})
        self.assertEqual(agents.findings(ctx, agent(ctx, "solo")), [])

    def test_two_vibe_agents_live_side_by_side(self):
        entries = {(f"{s}-{n}", n): t for n in ("a1", "b2") for s, t in (("moshi", MO), ("ocellio", OC))}
        ctx, _ = registered(self, entries, agents_=[{"name": "a1", "harness": "vibe"}, {"name": "b2", "harness": "vibe"}])
        for name in ("a1", "b2"):
            agents.setup(ctx, agent(ctx, name))
        self.assertIn("`a1`", (ctx.paths.agent_home("a1") / "vibe" / "AGENTS.md").read_text())
        self.assertIn("`b2`", (ctx.paths.agent_home("b2") / "vibe" / "AGENTS.md").read_text())

    def test_agy_config_is_private_and_asks_for_writes(self):
        ctx, _ = registered(self, {("ocellio-agy", "agy"): OC, ("moshi-agy", "agy"): MO})
        ctx.paths.agy_settings.parent.mkdir(parents=True)
        inherited = ["mine", "mcp(ocellio/update_entity)", "mcp(*)", "mcp(moshi/*)"]
        ctx.paths.agy_settings.write_text(json.dumps({"trustedWorkspaces": ["/x"], "permissions": {"allow": inherited}}))
        agents.setup(ctx, agent(ctx, "agy"))
        self.assertEqual(stat.S_IMODE(ctx.paths.agy_mcp_config.stat().st_mode), 0o600)
        servers = json.loads(ctx.paths.agy_mcp_config.read_text())["mcpServers"]
        self.assertEqual(servers["moshi"]["serverUrl"], MOSHI)
        self.assertIn("update_entity", servers["ocellio"]["enabledTools"])  # visible, agy asks
        allow = json.loads(ctx.paths.agy_settings.read_text())["permissions"]["allow"]
        self.assertIn("mine", allow)
        self.assertIn("mcp(ocellio/get_entity)", allow)
        for granting in ("mcp(ocellio/update_entity)", "mcp(*)", "mcp(moshi/*)"):
            self.assertNotIn(granting, allow)
        self.assertNotIn("agy-Config", " ".join(agents.findings(ctx, agent(ctx, "agy"))))
        self.assertFalse([f for f in agents.findings(ctx, agent(ctx, "agy")) if "weicht" in f or "0600" in f])

    def test_agy_findings_catch_changes_and_never_show_tokens(self):
        ctx, _ = registered(self, {("ocellio-agy", "agy"): OC, ("moshi-agy", "agy"): MO})
        agents.setup(ctx, agent(ctx, "agy"))
        data = json.loads(ctx.paths.agy_mcp_config.read_text())
        data["mcpServers"]["evil"] = {"serverUrl": "https://example.invalid/mcp"}
        ctx.paths.agy_mcp_config.write_text(json.dumps(data))
        os.chmod(ctx.paths.agy_mcp_config, 0o644)
        found = " | ".join(agents.findings(ctx, agent(ctx, "agy")))
        self.assertIn("weicht", found)
        self.assertIn("0600", found)
        self.assertNotIn(OC, found)
        self.assertNotIn(MO, found)

    def test_vibe_findings_catch_any_change(self):
        ctx, _ = registered(self, {("moshi-vibe", "vibe"): MO, ("ocellio-vibe", "vibe"): OC},
                            agents_=[dict(VIBE, home=str(temp_paths(self).home / "vibe-home"))])
        agents.setup(ctx, agent(ctx, "vibe"))
        config = Path(agent(ctx, "vibe").home) / "config.toml"
        original = config.read_text()
        for extra in ('\n[tools."extra_update_entity"]\npermission = "always"\n',
                      '\n[[mcp_servers]]\nname = "evil"\nurl = "https://example.invalid/mcp"\n'):
            config.write_text(original + extra)
            with self.subTest(extra=extra):
                self.assertTrue(any("weicht" in f for f in agents.findings(ctx, agent(ctx, "vibe"))))

    def test_missing_tokens_for_configured_servers_stop_setup(self):
        ctx, _ = registered(self)
        with self.assertRaises(ValueError) as caught:
            agents.setup(ctx, agent(ctx, "codex"))
        self.assertIn("moshi-codex", str(caught.exception))


class BotAddTest(unittest.TestCase):
    def test_add_stores_tokens_hidden_and_configures(self):
        ctx, fake = registered(self)
        answers = iter([MO, OC])
        io = bots.BotIO(ask_secret=lambda _p: next(answers), open_url=lambda _u: True, say=lambda _m: None)
        bots.add("vibe", ctx, io)
        self.assertEqual(fake.entries[("moshi-vibe", "vibe")], MO)
        self.assertEqual(fake.entries[("ocellio-vibe", "vibe")], OC)
        self.assertFalse(fake.argv_contains(MO) or fake.argv_contains(OC))
        self.assertTrue((ctx.paths.agent_home("vibe") / "vibe" / "config.toml").exists())

    def test_only_configured_servers_are_asked_for(self):
        ctx, fake = registered(self, servers={"moshi": MOSHI})
        io = bots.BotIO(ask_secret=lambda _p: MO, open_url=lambda _u: True, say=lambda _m: None)
        bots.add("codex", ctx, io)
        self.assertEqual(set(fake.entries), {("moshi-codex", "codex")})

    def test_unknown_agent_is_refused(self):
        ctx, _ = registered(self)
        io = bots.BotIO(ask_secret=lambda _p: self.fail("no prompt"), open_url=lambda _u: True, say=lambda _m: None)
        with self.assertRaises(ValueError):
            bots.add("nobody", ctx, io)

    def test_strict_hardens_existing_entries_instead_of_skipping(self):
        ctx, fake = registered(self, {("moshi-vibe", "vibe"): MO, ("ocellio-vibe", "vibe"): OC})
        io = bots.BotIO(ask_secret=lambda _p: self.fail("no prompt for existing entries"),
                        open_url=lambda _u: True, say=lambda _m: None)
        notes = bots.add("vibe", ctx, io, strict=True)
        self.assertTrue(all("gehärtet" in n for n in notes[:2]), notes)
        self.assertTrue(ctx.keychain.is_strict("moshi-vibe", "vibe"))
        self.assertEqual(fake.entries[("moshi-vibe", "vibe")], MO)
        again = bots.add("vibe", ctx, io, strict=True)
        self.assertIn("schon in der Keychain und gehärtet", again[0])

    def test_invalid_token_three_times_aborts(self):
        ctx, _ = registered(self)
        io = bots.BotIO(ask_secret=lambda _p: "bad token", open_url=lambda _u: True, say=lambda _m: None)
        with self.assertRaises(ValueError):
            bots.add("codex", ctx, io)


class DefaultsDoctorTest(unittest.TestCase):
    def test_telemetry_merge_keeps_other_keys(self):
        ctx, _ = context(self)
        ctx.paths.herdr_app_home.mkdir()
        ctx.paths.herdr_settings.write_text(json.dumps({"zoom": 1.2}))
        self.assertTrue(defaults.ensure_telemetry_off(ctx.paths, ctx.backup))
        self.assertFalse(defaults.ensure_telemetry_off(ctx.paths, ctx.backup))
        self.assertEqual(json.loads(ctx.paths.herdr_settings.read_text()), {"zoom": 1.2, "telemetry": False})

    def test_our_mods_are_installed_and_known(self):
        ctx, _ = context(self)
        changed = defaults.install_mods(ctx.paths, ctx.backup)
        self.assertIn("moshi/main.js", changed)
        self.assertEqual(defaults.install_mods(ctx.paths, ctx.backup), [])
        self.assertEqual(defaults.unknown_mods(ctx.paths), [])

    def test_old_panel_key_goes_only_where_it_is_still_ours(self):
        ctx, _ = context(self)
        ctx.paths.herdr_app_home.mkdir()
        keys = ctx.paths.herdr_app_home / "keys.json"
        keys.write_text(json.dumps({"global": {"cmd-t": "tab.new", "cmd-shift-m": "mod.open:moshi"},
                                    "layout:cockpit": {"cmd-shift-m": "layout.toggle:moshi"}}))
        self.assertIn("entfernt", defaults.remove_panel_key(ctx.paths, ctx.backup))
        self.assertEqual(json.loads(keys.read_text()), {"global": {"cmd-t": "tab.new"}})
        self.assertIsNone(defaults.remove_panel_key(ctx.paths, ctx.backup))
        keys.write_text(json.dumps({"global": {"cmd-shift-m": "tab.new"}}))
        self.assertIsNone(defaults.remove_panel_key(ctx.paths, ctx.backup))
        self.assertEqual(json.loads(keys.read_text())["global"]["cmd-shift-m"], "tab.new")

    def test_old_panel_key_leaves_a_broken_or_missing_file_alone(self):
        ctx, _ = context(self)
        self.assertIsNone(defaults.remove_panel_key(ctx.paths, ctx.backup))
        self.assertFalse((ctx.paths.herdr_app_home / "keys.json").exists())
        ctx.paths.herdr_app_home.mkdir()
        keys = ctx.paths.herdr_app_home / "keys.json"
        keys.write_text("{ kaputt")
        self.assertIsNone(defaults.remove_panel_key(ctx.paths, ctx.backup))
        self.assertEqual(keys.read_text(), "{ kaputt")
        self.assertEqual(defaults.cockpit_findings(ctx.paths), ["Layout cockpit fehlt"])

    def test_cockpit_layout_puts_own_agents_in_the_sidebar_and_leaves_main_untouched(self):
        ctx, _ = context(self)
        layouts = ctx.paths.herdr_app_home / "layouts"
        layouts.mkdir(parents=True)
        sidebar = {"id": "sidebar", "type": "column", "children": [
            {"id": "spaces", "content": {"mod": "spaces"}}, {"id": "agents", "content": {"mod": "agents"}}]}
        panel = {"id": "panel", "type": "column", "children": [{"id": "panel-body", "type": "stack", "children": [
            {"id": "files", "content": {"mod": "files"}}, {"id": "changes", "content": {"mod": "changes"}}]}]}
        work = {"id": "work", "type": "column", "children": [{"id": "main", "content": {"tab": "active"}}, panel]}
        main = {"id": "root", "type": "row", "children": [sidebar, work]}
        (layouts / "main.json").write_text(json.dumps(main))
        self.assertIn("angelegt", defaults.install_cockpit_layout(ctx.paths, ctx.backup))
        self.assertIn("unverändert", defaults.install_cockpit_layout(ctx.paths, ctx.backup))
        layout = json.loads((layouts / "cockpit.json").read_text())
        self.assertEqual([c["id"] for c in layout["children"]], ["sidebar", "work"])  # the right side stays empty
        self.assertEqual([c["id"] for c in layout["children"][0]["children"]], ["spaces", "agents", "own-agents"])
        self.assertEqual(layout["children"][0]["children"][-1]["content"], {"mod": "moshi"})
        self.assertEqual([c["id"] for c in layout["children"][1]["children"]], ["main"])  # files/changes left out
        self.assertEqual(json.loads((layouts / "main.json").read_text()), main)

    def test_cockpit_layout_skipped_without_main(self):
        ctx, _ = context(self)
        self.assertIn("übersprungen", defaults.install_cockpit_layout(ctx.paths, ctx.backup))

    def test_status_json_is_sanitized_and_compact(self):
        from cockpit import status
        raw = {"inbox_pending": 2, "agents": [{"name": "x\x1b[2J", "presence": "live", "webhook": {"url": "secret"}}]}
        data = json.loads(status.as_json(raw, "cockpit", [{"name": "a"}], {"moshi": True}))
        self.assertNotIn("inbox_pending", data)
        self.assertNotIn("\x1b", data["agents"][0]["name"])
        self.assertNotIn("webhook", data["agents"][0])
        self.assertEqual(data["local"], [{"name": "a"}])
        self.assertEqual(data["servers"], {"moshi": True})

    def test_local_agents_carry_name_harness_and_moshi_name(self):
        from cockpit import config, status
        ctx, _ = registered(self, person="anna", agents_=[{"name": "research", "harness": "codex", "role": "R"}])
        local = status.local_agents(ctx.paths, config.load_config(ctx.paths), ctx.keychain)
        self.assertEqual([(a["name"], a["harness"], a["moshi_name"], a["role"]) for a in local],
                         [("research", "codex", "anna-research", "R")])
        ctx2, _ = registered(self, servers={}, agents_=[{"name": "solo", "harness": "vibe"}])
        self.assertEqual(status.local_agents(ctx2.paths, config.load_config(ctx2.paths), ctx2.keychain)[0]["moshi_name"], "")

    def test_unknown_mods_reported(self):
        ctx, _ = context(self)
        (ctx.paths.herdr_mods / "files").mkdir(parents=True)
        (ctx.paths.herdr_mods / "sketchy").mkdir()
        ctx.paths.herdr_installed.write_text(json.dumps({"files": {}, "layout:main": {}}))
        self.assertEqual(defaults.unknown_mods(ctx.paths), ["sketchy"])

    def test_group_writable_app_home_is_a_finding(self):
        ctx, _ = context(self)
        ctx.paths.herdr_app_home.mkdir()
        os.chmod(ctx.paths.herdr_app_home, 0o775)
        self.assertTrue(defaults.permission_findings(ctx.paths))

    def test_socket_must_be_a_socket_of_yours_in_a_folder_of_yours(self):
        import socket
        ctx, _ = context(self)
        ctx.paths.herdr_app_home.mkdir(mode=0o755)
        uid = os.getuid()
        self.assertEqual(defaults.socket_findings(ctx.paths.herdr_socket, uid), [])  # not running: fine
        ctx.paths.herdr_socket.write_text("")  # a plain file where the socket should be
        self.assertTrue(any("kein Socket" in f for f in defaults.socket_findings(ctx.paths.herdr_socket, uid)))
        ctx.paths.herdr_socket.unlink()
        server = socket.socket(socket.AF_UNIX)
        self.addCleanup(server.close)
        tmp = tempfile.TemporaryDirectory(dir="/tmp")  # AF_UNIX paths are short; keep this one under /tmp
        self.addCleanup(tmp.cleanup)
        short = Path(tmp.name)
        os.chmod(short, 0o755)
        server.bind(str(short / "s.sock"))
        self.assertEqual(defaults.socket_findings(short / "s.sock", uid), [])
        self.assertTrue(any("gehört nicht dir" in f for f in defaults.socket_findings(short / "s.sock", uid + 1)))
        os.chmod(short, 0o777)
        self.assertTrue(any("Socket austauschbar" in f for f in defaults.socket_findings(short / "s.sock", uid)))
        os.chmod(short, 0o755)

    def test_doctor_exit_code_and_version_drift_is_warning(self):
        ctx, _ = registered(self)
        ctx.paths.herdr_app_home.mkdir()
        ctx.paths.herdr_settings.write_text(json.dumps({"telemetry": False}))
        dctx = doctor.DoctorContext(paths=ctx.paths, keychain=ctx.keychain, pins=load_pins(),
                                    probe=lambda _url: (401, "Bearer x"), core_version=lambda _p: (0, 1, 99),
                                    bundle=lambda c, _p: Path("/x/Herdr Core.app") if c == "herdr-core" else None,
                                    codesign=lambda _b, _pin: None)
        results = {r.name: r for r in doctor.run(dctx)}
        self.assertEqual(results["herdr-core"].status, doctor.WARN)
        bad = doctor.DoctorContext(paths=ctx.paths, keychain=ctx.keychain, pins=load_pins(),
                                   probe=lambda _url: (401, "Bearer x"), core_version=lambda _p: (0, 1, 18),
                                   bundle=lambda c, _p: Path("/x/Herdr Core.app"),
                                   codesign=lambda _b, pin: (_ for _ in ()).throw(PinError("Team falsch")))
        core = {r.name: r for r in doctor.run(bad)}["herdr-core"]
        self.assertEqual(core.status, doctor.FAIL)
        self.assertIn("Team falsch", core.detail)
        self.assertEqual(results["herdr-app"].status, doctor.FAIL)  # not installed in temp HOME
        self.assertEqual(results["telemetry"].status, doctor.OK)
        self.assertEqual(results["cockpit"].status, doctor.WARN)
        self.assertEqual(results["endpoint-moshi"].status, doctor.OK)
        self.assertEqual(doctor.exit_code(list(results.values())), 1)

    def test_doctor_config_and_endpoints_follow_the_configured_servers(self):
        ctx, _ = context(self)
        probed = []
        dctx = doctor.DoctorContext(paths=ctx.paths, keychain=ctx.keychain, pins=load_pins(),
                                    probe=lambda url: probed.append(url) or (401, "Bearer"))
        self.assertEqual(doctor.check_config(dctx)[0].status, doctor.OK)
        self.assertEqual(doctor.check_endpoints(dctx), [])
        self.assertEqual(probed, [])  # no servers: no network at all
        configure(ctx.paths, servers={"moshi": MOSHI})
        self.assertEqual([c.name for c in doctor.check_endpoints(dctx)], ["endpoint-moshi"])
        ctx.paths.mesh_config.write_text(json.dumps({"servers": {"moshi": "http://insecure.example/mcp"}}))
        self.assertEqual(doctor.check_config(dctx)[0].status, doctor.FAIL)
        ctx.paths.mesh_config.write_text(json.dumps({"servers": {}}))
        os.chmod(ctx.paths.mesh_config, 0o644)
        perms = doctor.check_config(dctx)[0]
        self.assertEqual(perms.status, doctor.FAIL)
        self.assertIn("0600", perms.detail)
        os.chmod(ctx.paths.mesh_config, 0o600)
        ctx.paths.mesh_config.write_text("{ kaputt")
        self.assertEqual(doctor.check_config(dctx)[0].status, doctor.FAIL)

    def test_doctor_reports_parked_keychain_copies(self):
        ctx, _ = registered(self, {("moshi-vibe", "vibe"): MO, ("moshi-vibe.pending", "vibe"): MO,
                                   ("ocellio-agy.pending", "agy"): OC})
        results = {r.name: r for r in doctor.check_parked(doctor.DoctorContext(
            paths=ctx.paths, keychain=ctx.keychain, pins=load_pins()))}
        parked = results["keychain-parked"]
        self.assertEqual(parked.status, doctor.WARN)
        self.assertIn("abgebrochenen Lauf", parked.detail)   # next to an existing entry
        self.assertIn("stellt sie wieder her", parked.detail)  # entry missing, copy there
        self.assertNotIn(MO, parked.detail)

    def test_doctor_json_never_contains_tokens(self):
        ctx, _ = registered(self, {("ocellio-vibe", "vibe"): OC, ("moshi-vibe", "vibe"): MO})
        dctx = doctor.DoctorContext(paths=ctx.paths, keychain=ctx.keychain, pins=load_pins(),
                                    probe=lambda _url: (401, "Bearer"), core_version=lambda _p: None)
        out = doctor.render(doctor.run(dctx), as_json=True)
        self.assertNotIn(OC, out)
        self.assertNotIn(MO, out)


class AgentCommandTest(unittest.TestCase):
    def test_add_list_remove_and_rules(self):
        import io as _io
        from contextlib import redirect_stdout
        from cockpit import cli
        paths = temp_paths(self)
        env = {"HOME": str(paths.home)}
        with mock.patch.dict(os.environ, env), mock.patch("cockpit.cli.Keychain", return_value=Keychain(FakeSecurity())):
            out = _io.StringIO()
            with redirect_stdout(out):
                self.assertEqual(cli.main(["agent", "add", "research", "--harness", "codex", "--role", "Recherche"]), 0)
                self.assertEqual(cli.main(["agent", "add", "ag1", "--harness", "agy"]), 0)
            self.assertEqual(cli.main(["agent", "add", "ag2", "--harness", "agy"]), 1)      # one agy per machine
            self.assertEqual(cli.main(["agent", "add", "RESEARCH", "--harness", "vibe"]), 1)  # duplicate name
            self.assertEqual(cli.main(["agent", "add", "cockpit", "--harness", "vibe"]), 1)   # reserved
            self.assertEqual(cli.main(["agent", "add", "x", "--harness", "emacs"]), 1)
            names = [a.name for a in registry.load_agents(paths)]
            self.assertEqual(names, ["research", "ag1"])
            with redirect_stdout(_io.StringIO()):
                self.assertEqual(cli.main(["agent", "remove", "ag1"]), 0)
            self.assertEqual([a.name for a in registry.load_agents(paths)], ["research"])


class InstallerTest(unittest.TestCase):
    def test_fresh_install_has_no_servers_and_no_agents(self):
        ctx, fake = context(self)
        notes = installer.run(ctx, load_pins(), installer.InstallOptions(skip_herdr=True))
        self.assertFalse(ctx.paths.agents_file.exists())
        self.assertFalse(any("Migration" in n for n in notes))
        self.assertTrue((ctx.paths.local_bin / "moshi-cockpit").is_symlink())

    def test_server_flags_are_validated_and_saved(self):
        from cockpit import config
        ctx, _ = context(self)
        installer.run(ctx, load_pins(), installer.InstallOptions(skip_herdr=True, moshi=MOSHI))
        self.assertEqual(config.load_config(ctx.paths).servers, {"moshi": MOSHI})
        for bad in ("http://x.example/mcp", "https://user:pw@example.test/mcp", "https://x.example/mcp?a=1",
                    "https://x.example/mcp#f", "https:///mcp", "https://x.example:99999/mcp", "https://x .example/mcp"):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                installer.run(ctx, load_pins(), installer.InstallOptions(skip_herdr=True, moshi=bad))
        installer.run(ctx, load_pins(), installer.InstallOptions(skip_herdr=True, moshi="https://intern.example:8443/mcp"))
        self.assertEqual(config.load_config(ctx.paths).servers["moshi"], "https://intern.example:8443/mcp")  # port ok
        installer.run(ctx, load_pins(), installer.InstallOptions(skip_herdr=True, moshi=""))
        self.assertEqual(config.load_config(ctx.paths).servers, {})

    def test_first_generation_installation_is_carried_over(self):
        from cockpit import config
        ctx, fake = context(self, {("ocellio-vibe", "vibe"): OC, ("moshi-vibe", "vibe"): MO,
                                   ("moshi-cockpit", "cockpit"): MO, ("moshi-agy", "agy"): MO, ("ocellio-agy", "agy"): OC})
        ctx.paths.agy_mcp_config.parent.mkdir(parents=True)
        ctx.paths.agy_mcp_config.write_text(json.dumps({"mcpServers": {
            "ocellio": {"serverUrl": OCELLIO}, "moshi": {"serverUrl": MOSHI}}}))
        notes = installer.run(ctx, load_pins(), installer.InstallOptions(skip_herdr=True))
        self.assertTrue(any("Migration: Agents übernommen (vibe, agy)" in n for n in notes), notes)
        self.assertEqual(config.load_config(ctx.paths).servers, {"moshi": MOSHI, "ocellio": OCELLIO})
        vibe = registry.find(ctx.paths, "vibe")
        self.assertEqual(vibe.home, str(ctx.paths.legacy_vibe_home))
        self.assertTrue((ctx.paths.legacy_vibe_home / "config.toml").exists())
        self.assertEqual(fake.entries[("moshi-vibe", "vibe")], MO)  # keychain untouched
        again = installer.run(ctx, load_pins(), installer.InstallOptions(skip_herdr=True))
        self.assertFalse(any("Migration" in n for n in again))  # once only
        ctx.backup.restore_all()
        self.assertFalse(ctx.paths.agents_file.exists())


if __name__ == "__main__":
    unittest.main()
