import importlib.util
import io
import json
import os
import stat
import tempfile
import unittest
from contextlib import redirect_stderr
from unittest import mock
from pathlib import Path

from cockpit import agents, cli, compat, defaults, doctor, registry
from cockpit.agents import AgentContext
from cockpit.backup import BackupStore
from cockpit.secrets.macos import MacKeychain
from tests.helpers import MOSHI, OCELLIO, FakeSecurity, bind_non_agent, configure, herdr_app, install, temp_paths

OC, MO = "oc_TOKEN_aaaaaaaa", "bt_TOKEN_bbbbbbbb"


def context(test, entries=None):
    paths = temp_paths(test)
    fake = FakeSecurity(entries or {})
    return AgentContext(paths=paths, backup=BackupStore(paths.state), keychain=MacKeychain(fake)), fake


VIBE = {"name": "vibe", "harness": "vibe"}
AGY = {"name": "agy", "harness": "agy"}
CODEX = {"name": "codex", "harness": "codex"}


def registered(test, entries=None, servers=None, agents_=(VIBE, AGY, CODEX)):
    # The CLIs themselves are not installed in CI; whether they are is not what these tests check.
    patcher = mock.patch("cockpit.harnesses.base.Harness.installed", return_value=True)
    patcher.start()
    test.addCleanup(patcher.stop)
    ctx, fake = context(test, entries)
    configure(ctx.paths, servers=servers, agents=agents_)
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
        data = json.loads(status.as_json(status.mesh_from(raw), [{"name": "a"}], {"moshi": True}))
        self.assertNotIn("inbox_pending", data)
        self.assertNotIn("\x1b", data["agents"][0]["name"])
        self.assertNotIn("webhook", data["agents"][0])
        self.assertEqual(data["local"], [{"name": "a"}])
        self.assertEqual(data["servers"], {"moshi": True})

    def test_local_agents_carry_name_harness_and_role(self):
        from cockpit import config, status
        ctx, _ = registered(self, agents_=[{"name": "research", "harness": "codex", "role": "R"}])
        local = status.local_agents(ctx.paths, config.load_config(ctx.paths), ctx.keychain)
        self.assertEqual([(a["name"], a["harness"], a["role"]) for a in local], [("research", "codex", "R")])
        self.assertNotIn("moshi_name", local[0])  # an agent's moshi name is its name

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

    def test_doctor_exit_code(self):
        ctx, _ = registered(self)
        ctx.paths.herdr_app_home.mkdir()
        ctx.paths.herdr_settings.write_text(json.dumps({"telemetry": False}))
        dctx = doctor.DoctorContext(paths=ctx.paths, keychain=ctx.keychain, probe=lambda _url: (401, "Bearer x"),
                                    herdr_versions=lambda _p: (None, None))
        results = {r.name: r for r in doctor.run(dctx)}
        self.assertEqual(results["herdr"].status, doctor.FAIL)  # not installed in temp HOME
        self.assertEqual(results["telemetry"].status, doctor.OK)
        self.assertEqual(results["cockpit"].status, doctor.WARN)
        self.assertEqual(results["endpoint-moshi"].status, doctor.OK)
        self.assertEqual(doctor.exit_code(list(results.values())), 1)

    def test_doctor_config_and_endpoints_follow_the_configured_servers(self):
        ctx, _ = context(self)
        probed = []
        dctx = doctor.DoctorContext(paths=ctx.paths, keychain=ctx.keychain,
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
            paths=ctx.paths, keychain=ctx.keychain))}
        parked = results["keychain-parked"]
        self.assertEqual(parked.status, doctor.WARN)
        self.assertIn("interrupted run", parked.detail)   # next to an existing entry
        self.assertIn("store the token again", parked.detail)  # entry missing, copy there
        self.assertNotIn(MO, parked.detail)

    def test_doctor_json_never_contains_tokens(self):
        ctx, _ = registered(self, {("ocellio-vibe", "vibe"): OC, ("moshi-vibe", "vibe"): MO})
        dctx = doctor.DoctorContext(paths=ctx.paths, keychain=ctx.keychain, probe=lambda _url: (401, "Bearer"),
                                    herdr_versions=lambda _p: (None, None))
        out = doctor.render(doctor.run(dctx), as_json=True)
        self.assertNotIn(OC, out)
        self.assertNotIn(MO, out)


class AgentCommandTest(unittest.TestCase):
    def test_add_with_moshi_but_no_approval_exits_3_and_creates_nothing(self):
        import io as _io
        paths = temp_paths(self)
        configure(paths)  # moshi and ocellio configured, this Herdr not connected
        with mock.patch("cockpit.cli.Paths.from_env", return_value=paths), \
                mock.patch("cockpit.cli.default_store", return_value=MacKeychain(FakeSecurity())), \
                mock.patch("cockpit.harnesses.base.Harness.installed", return_value=True), \
                redirect_stderr(_io.StringIO()) as err:
            self.assertEqual(cli.main(["agent", "add", "x", "--harness", "codex"]), 3)
        self.assertIn("moshi-cockpit connect", err.getvalue())
        self.assertEqual(registry.load_agents(paths), [])

    def test_add_list_remove_and_rules(self):
        import io as _io
        from contextlib import redirect_stdout
        paths = temp_paths(self)
        env = {"HOME": str(paths.home)}
        with mock.patch.dict(os.environ, env), mock.patch("cockpit.cli.default_store", return_value=MacKeychain(FakeSecurity())):
            out = _io.StringIO()
            with redirect_stdout(out):
                self.assertEqual(cli.main(["agent", "add", "research", "--harness", "codex", "--role", "Recherche"]), 0)
                self.assertEqual(cli.main(["agent", "add", "ag1", "--harness", "agy"]), 0)
            with redirect_stderr(_io.StringIO()):
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
        notes = install(ctx)
        self.assertFalse(ctx.paths.agents_file.exists())
        self.assertFalse(any("Migration" in n for n in notes))
        self.assertTrue((ctx.paths.local_bin / "moshi-cockpit").is_symlink())

    def test_server_flags_are_validated_and_saved(self):
        from cockpit import config
        ctx, _ = context(self)
        install(ctx, moshi=MOSHI)
        self.assertEqual(config.load_config(ctx.paths).servers, {"moshi": MOSHI})
        for bad in ("http://x.example/mcp", "https://user:pw@example.test/mcp", "https://x.example/mcp?a=1",
                    "https://x.example/mcp#f", "https:///mcp", "https://x.example:99999/mcp", "https://x .example/mcp"):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                install(ctx, moshi=bad)
        install(ctx, moshi="https://intern.example:8443/mcp")
        self.assertEqual(config.load_config(ctx.paths).servers["moshi"], "https://intern.example:8443/mcp")  # port ok
        install(ctx, moshi="")
        self.assertEqual(config.load_config(ctx.paths).servers, {})

    def test_first_generation_installation_is_carried_over(self):
        from cockpit import config
        ctx, fake = context(self, {("ocellio-vibe", "vibe"): OC, ("moshi-vibe", "vibe"): MO,
                                   ("moshi-cockpit", "cockpit"): MO, ("moshi-agy", "agy"): MO, ("ocellio-agy", "agy"): OC})
        ctx.paths.agy_mcp_config.parent.mkdir(parents=True)
        ctx.paths.agy_mcp_config.write_text(json.dumps({"mcpServers": {
            "ocellio": {"serverUrl": OCELLIO}, "moshi": {"serverUrl": MOSHI}}}))
        notes = install(ctx)
        self.assertTrue(any("Migration: agents carried over (vibe, agy)" in n for n in notes), notes)
        self.assertEqual(config.load_config(ctx.paths).servers, {"moshi": MOSHI, "ocellio": OCELLIO})
        vibe = registry.find(ctx.paths, "vibe")
        self.assertEqual(vibe.home, str(ctx.paths.legacy_vibe_home))
        self.assertTrue((ctx.paths.legacy_vibe_home / "config.toml").exists())
        self.assertEqual(fake.entries[("moshi-vibe", "vibe")], MO)  # keychain untouched
        again = install(ctx)
        self.assertFalse(any("Migration" in n for n in again))  # once only
        ctx.backup.restore_all()
        self.assertFalse(ctx.paths.agents_file.exists())


class PlainStore:
    """A SecretStore with only the four protocol methods: no macOS hardening extras."""

    def __init__(self, entries=None):
        self.entries = dict(entries or {})

    def exists(self, service, account):
        return (service, account) in self.entries

    def read(self, service, account):
        return self.entries[(service, account)]

    def write(self, service, account, secret, strict=False):
        self.entries[(service, account)] = secret

    def delete(self, service, account):
        del self.entries[(service, account)]


class HerdrFromTheVendorTest(unittest.TestCase):
    """Herdr comes from herdr.dev; moshi-cockpit only checks what is installed."""

    def doctor(self, ctx, app, core, keychain=None):
        return doctor.DoctorContext(paths=ctx.paths, keychain=keychain or ctx.keychain,
                                    probe=lambda _url: (401, "Bearer"), herdr_versions=lambda _p: (app, core))

    def test_doctor_warns_on_an_untested_herdr_version_and_does_not_fail(self):
        ctx, _ = context(self)
        results = doctor.check_herdr(self.doctor(ctx, "0.2.0", "0.9.9"))
        by_name = {r.name: r for r in results}
        self.assertEqual(by_name["herdr"].status, doctor.WARN)
        self.assertIn("0.2.0", by_name["herdr"].detail)
        self.assertIn(compat.TESTED_HERDR_APP[0], by_name["herdr"].detail)
        self.assertEqual(by_name["herdr-core"].status, doctor.WARN)
        self.assertEqual(doctor.exit_code(results), 0)
        self.assertIsNone(doctor.quiet(results))
        tested = doctor.check_herdr(self.doctor(ctx, compat.TESTED_HERDR_APP[0], compat.TESTED_HERDR_CORE[0]))
        self.assertEqual({r.status for r in tested}, {doctor.OK})

    def test_doctor_fails_without_herdr_and_names_herdr_dev(self):
        ctx, _ = context(self)
        results = doctor.check_herdr(self.doctor(ctx, None, "0.1.18"))
        self.assertEqual([(r.name, r.status) for r in results], [("herdr", doctor.FAIL)])
        self.assertIn("https://herdr.dev", doctor.quiet(results))

    def test_an_installed_herdr_with_an_odd_or_unreadable_version_only_warns(self):
        ctx, _ = context(self)
        for version, shown in (("0.2", "0.2"), ("1.0.0-rc1", "1.0.0-rc1"), ("0.2\x1b[2J", "0.2[2J"), ("", "unknown")):
            with self.subTest(version=version):
                herdr_app(ctx.paths, version=version, ran_before=False)
                self.assertEqual(compat.installed_versions(ctx.paths), (shown, None))
        (ctx.paths.herdr_app_bundle / "Contents" / "Info.plist").unlink()
        self.assertEqual(compat.installed_versions(ctx.paths), ("unknown", None))
        (ctx.paths.herdr_app_bundle / "Contents" / "Info.plist").write_text("broken")
        self.assertEqual(compat.installed_versions(ctx.paths), ("unknown", None))
        results = doctor.check_herdr(doctor.DoctorContext(paths=ctx.paths, keychain=ctx.keychain))
        self.assertEqual([(r.name, r.status) for r in results], [("herdr", doctor.WARN)])
        self.assertIn("unknown", results[0].detail)
        self.assertEqual(doctor.exit_code(results), 0)
        import shutil
        shutil.rmtree(ctx.paths.herdr_app_bundle)  # only a missing bundle is an error
        self.assertEqual(compat.installed_versions(ctx.paths), (None, None))

    def test_compat_reads_the_versions_from_the_bundles(self):
        ctx, _ = context(self)
        self.assertEqual(compat.installed_versions(ctx.paths), (None, None))
        herdr_app(ctx.paths, version="0.1.13")
        self.assertEqual(compat.installed_versions(ctx.paths), ("0.1.13", None))
        self.assertEqual(compat.check("0.1.13", None), [compat.Finding("herdr", compat.OK, "Herdr 0.1.13 (tested)")])

    def test_doctor_has_no_admin_or_cockpit_identity_checks(self):
        token = "bt_TOKEN_cccccccc"
        ctx, _ = context(self, {(s, a): token for s, a in (("moshi-cockpit", "cockpit"), ("moshi-admin", "admin"),
                                                            ("moshi-register", "register"), ("moshi-device", "device"))})
        configure(ctx.paths, servers={"moshi": MOSHI})
        for identity in ("cockpit", "admin", "register"):
            bind_non_agent(ctx.paths, MOSHI, identity)
        names = [r.name for r in doctor.run(self.doctor(ctx, "0.1.13", None))]
        for gone in ("identity-cockpit", "identity-admin", "identity-register"):
            self.assertNotIn(gone, names)
        device = {r.name: r for r in doctor.check_identities(self.doctor(ctx, "0.1.13", None))}["identity-device"]
        self.assertEqual(device.status, doctor.FAIL)  # stored, but bound to no server: no token goes out
        bind_non_agent(ctx.paths, MOSHI, "device")
        device = {r.name: r for r in doctor.check_identities(self.doctor(ctx, "0.1.13", None))}["identity-device"]
        self.assertEqual(device.status, doctor.OK)
        self.assertNotIn(token, device.detail)

    def test_doctor_works_with_a_store_without_macos_extras(self):
        ctx, _ = registered(self, agents_=[VIBE])
        results = doctor.run(self.doctor(ctx, "0.1.13", None, keychain=PlainStore()))
        self.assertNotIn("keychain-parked", [r.name for r in results])
        self.assertEqual(doctor.check_parked(self.doctor(ctx, "0.1.13", None, keychain=PlainStore())), [])

    def test_cli_has_no_bot_or_test_commands(self):
        commands = set(cli.build_parser()._subparsers._group_actions[0].choices)
        self.assertEqual(commands, {"install", "uninstall", "connect", "run", "doctor", "status", "agent", "rules",
                                    "sessions", "harness"})
        for gone in (["bot", "add", "x"], ["bot", "admin-token"], ["bot", "register-token", "list"], ["test"]):
            with self.subTest(argv=gone), redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as caught:
                cli.main(gone)
            self.assertEqual(caught.exception.code, 2)
        for module in ("admin", "agenttest", "bots", "herdr", "pins", "register"):
            self.assertIsNone(importlib.util.find_spec(f"cockpit.{module}"), module)


if __name__ == "__main__":
    unittest.main()
