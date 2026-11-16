"""Acceptance criteria for vendor-neutral agents (no agent CLI or server built in), one test each."""

import io
import json
import os
import socket
import unittest
from contextlib import redirect_stderr, redirect_stdout
from unittest import mock

from cockpit import agents, config, doctor, installer, launch, registry
from cockpit.secrets.macos import MacKeychain
from tests.helpers import FOREIGN, MOSHI, OCELLIO, OWN, bind_non_agent, configure, context, herdr_app, start, temp_paths


def install(ctx, **options):
    """`install` on a Mac where Herdr.app is installed and ran before."""
    herdr_app(ctx.paths)
    return installer.run(ctx, installer.InstallOptions(**options))


class OriginBindingTest(unittest.TestCase):
    def setUp(self):
        self.ctx, _ = context(self, {("moshi-research", "research"): OWN, ("ocellio-research", "research"): OWN})
        configure(self.ctx.paths, agents=[{"name": "research", "harness": "codex"}])

    def test_a_changed_server_url_sends_no_token(self):
        self.assertEqual(start(self.ctx, "research")["env"]["MESH_MOSHI_TOKEN"], OWN)
        data = json.loads(self.ctx.paths.mesh_config.read_text())
        data["servers"]["moshi"] = "https://evil.example/mcp"
        self.ctx.paths.mesh_config.write_text(json.dumps(data))
        with self.assertRaises(ValueError) as caught:
            start(self.ctx, "research")
        self.assertIn("kein Token gesetzt", str(caught.exception))
        self.assertNotIn(OWN, str(caught.exception))
        found = agents.findings(self.ctx, registry.find(self.ctx.paths, "research"))
        self.assertTrue(any(f.startswith("Origin-Mismatch moshi") for f in found), found)

    def test_the_default_port_is_the_same_origin(self):
        self.assertEqual(config.origin("https://h.example/mcp"), config.origin("https://h.example:443/mcp"))
        self.assertNotEqual(config.origin("https://h.example/mcp"), config.origin("https://h.example:8443/mcp"))
        data = json.loads(self.ctx.paths.mesh_config.read_text())
        data["servers"]["moshi"] = MOSHI.replace("moshi.example.test", "moshi.example.test:443")
        self.ctx.paths.mesh_config.write_text(json.dumps(data))
        self.assertEqual(start(self.ctx, "research")["env"]["MESH_MOSHI_TOKEN"], OWN)  # no false alarm

    def test_a_port_or_host_case_change_is_judged_by_origin(self):
        data = json.loads(self.ctx.paths.mesh_config.read_text())
        data["servers"]["moshi"] = MOSHI.replace("moshi.example.test", "MOSHI.example.test")
        self.ctx.paths.mesh_config.write_text(json.dumps(data))
        self.assertEqual(start(self.ctx, "research")["env"]["MESH_MOSHI_TOKEN"], OWN)  # same origin
        data["servers"]["moshi"] = MOSHI.replace("moshi.example.test", "moshi.example.test:8443")
        self.ctx.paths.mesh_config.write_text(json.dumps(data))
        with self.assertRaises(ValueError):
            start(self.ctx, "research")

    def test_rebinding_needs_the_explicit_flag(self):
        moved = {"moshi": "https://moshi.new.example"}
        with self.assertRaises(ValueError):
            registry.bind(self.ctx.paths, self.ctx.backup, "research", moved)
        self.assertEqual(registry.find(self.ctx.paths, "research").bound["moshi"], "https://moshi.example.test")
        registry.bind(self.ctx.paths, self.ctx.backup, "research", moved, rebind=True)
        self.assertEqual(registry.find(self.ctx.paths, "research").bound["moshi"], "https://moshi.new.example")

    def test_an_unbound_agent_gets_no_token(self):
        configure(self.ctx.paths, agents=[{"name": "research", "harness": "codex"}], bind=False)
        with self.assertRaises(ValueError):
            start(self.ctx, "research")


class NonAgentTokenTest(unittest.TestCase):  # the device key and the registration token too
    def test_device_and_registration_tokens_only_go_to_their_bound_origin(self):
        from cockpit import tokens
        ctx, _ = context(self, {("moshi-device", "device"): OWN, ("moshi-register", "register"): OWN})
        configure(ctx.paths, servers={"moshi": MOSHI})
        for identity in (tokens.DEVICE, tokens.REGISTER):
            with self.subTest(identity=identity):
                with self.assertRaises(ValueError) as caught:  # not bound yet: nothing goes out
                    tokens.moshi_token(ctx.paths, ctx.keychain, identity, MOSHI)
                self.assertIn("moshi-cockpit connect", str(caught.exception))
                bind_non_agent(ctx.paths, MOSHI, identity)
                self.assertEqual(tokens.moshi_token(ctx.paths, ctx.keychain, identity, MOSHI), OWN)
                with self.assertRaises(ValueError) as caught:
                    tokens.moshi_token(ctx.paths, ctx.keychain, identity, "https://evil.example/mcp")
                self.assertNotIn(OWN, str(caught.exception))
        with self.assertRaises(ValueError):  # the person's ocellio token is never sent to moshi
            tokens.moshi_token(ctx.paths, ctx.keychain, tokens.OCELLIO_DEFAULT, MOSHI)

    def test_the_non_agent_tokens_and_their_binding(self):
        from cockpit import tokens
        self.assertEqual(tokens.NON_AGENT, ("device", "register", "ocellio-default"))
        ctx, _ = context(self)
        configure(ctx.paths, servers={"moshi": MOSHI})
        self.assertEqual(tokens.bind_identity(ctx.paths, ctx.backup, "device", MOSHI), "https://moshi.example.test")
        with self.assertRaises(ValueError):  # no silent move
            tokens.bind_identity(ctx.paths, ctx.backup, "device", "https://moshi.new.example/mcp")
        tokens.bind_identity(ctx.paths, ctx.backup, "device", "https://moshi.new.example/mcp", rebind=True)
        self.assertEqual(tokens.bound_origin(ctx.paths, "device"), "https://moshi.new.example")
        for gone in ("cockpit", "admin"):  # former helper identities are no longer bound here
            with self.subTest(gone=gone), self.assertRaises(ValueError):
                tokens.bind_identity(ctx.paths, ctx.backup, gone, MOSHI)


class FilePermissionTest(unittest.TestCase):
    def test_config_and_agent_list_must_be_owner_only_regular_files(self):
        ctx, _ = context(self)
        configure(ctx.paths, agents=[{"name": "a", "harness": "vibe"}])
        for path, load in ((ctx.paths.mesh_config, config.load_config), (ctx.paths.agents_file, registry.load_agents)):
            for mode in (0o644, 0o620, 0o604):
                os.chmod(path, mode)
                with self.subTest(path=path.name, mode=oct(mode)), self.assertRaises(ValueError):
                    load(ctx.paths)
            os.chmod(path, 0o600)
            load(ctx.paths)
        real = ctx.paths.agents_file
        moved = real.with_name("elsewhere.json")
        real.rename(moved)
        real.symlink_to(moved)
        with self.assertRaises(ValueError):
            registry.load_agents(ctx.paths)
        dctx = doctor.DoctorContext(paths=ctx.paths, keychain=ctx.keychain)
        self.assertEqual({r.name: r.status for r in doctor.check_agents(dctx)}["agents"], doctor.FAIL)

    def test_the_file_is_read_through_the_descriptor_that_was_checked(self):
        ctx, _ = context(self)
        configure(ctx.paths, agents=[{"name": "a", "harness": "vibe"}])
        opened = []
        real_open = os.open

        def spy(path, flags, *rest):
            opened.append((str(path), flags))
            return real_open(path, flags, *rest)
        with mock.patch.object(os, "open", spy):
            config.load_config(ctx.paths)
            registry.load_agents(ctx.paths)
        for path in (ctx.paths.mesh_config, ctx.paths.agents_file):
            flags = [f for p, f in opened if p == str(path)]
            self.assertTrue(flags and all(f & os.O_NOFOLLOW for f in flags), (path, flags))
        with mock.patch("pathlib.Path.read_text", side_effect=AssertionError("must read via the fd")):
            config.load_config(ctx.paths)
            registry.load_agents(ctx.paths)


class UrlTest(unittest.TestCase):
    def test_urls_are_parsed_not_pattern_matched(self):
        for good in ("https://moshi.example/mcp", "https://intern:8443/mcp", "https://10.0.0.5/mcp/", "https://h/"):
            self.assertEqual(config.validate_url(good), good)
        for bad in ("http://h/mcp", "https://u@h/mcp", "https://u:p@h/mcp", "https://h/mcp?x=1", "https://h/mcp#f",
                    "https:///mcp", "https://h:0x50/mcp", "ftp://h/mcp", "https://h/m cp", "https://h/\nmcp", ""):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                config.validate_url(bad)


class ChildEnvironmentTest(unittest.TestCase):
    def test_only_the_agents_own_token_reaches_the_child(self):
        ctx, _ = context(self, {("moshi-research", "research"): OWN, ("ocellio-research", "research"): OWN,
                                ("moshi-claude", "claude"): OWN})
        configure(ctx.paths, agents=[{"name": "research", "harness": "codex"}, {"name": "claude", "harness": "claude"}])
        inherited = {"PATH": "/usr/bin", "MESH_MOSHI_TOKEN": FOREIGN, "MESH_OCELLIO_TOKEN": FOREIGN,
                     "MOSHI_VIBE_TOKEN": FOREIGN, "OCELLIO_CODEX_TOKEN": FOREIGN, "SSH_AUTH_SOCK": "/x"}
        for name in ("research", "claude"):
            seen = start(ctx, name, ["hi"], environ=inherited)
            with self.subTest(name=name):
                self.assertNotIn(FOREIGN, json.dumps(seen["env"]))
                self.assertNotIn(OWN, " ".join(seen["argv"]))
                self.assertNotIn(FOREIGN, " ".join(seen["argv"]))
        self.assertEqual(start(ctx, "research", environ=inherited)["env"]["MESH_MOSHI_TOKEN"], OWN)
        self.assertEqual(start(ctx, "claude", environ=inherited)["env"]["MESH_MOSHI_TOKEN"], OWN)


class OwnMoshiIdentityTest(unittest.TestCase):  # one agent, one moshi identity, also claude and grok
    NAMES = ["writer", "Zed-2", "a_b", "x" * 64]

    def ctx(self):
        entries = {(f"moshi-{n}", n): OWN for n in self.NAMES}
        ctx, fake = context(self, {**entries, ("moshi-research", "research"): OWN, ("ocellio-research", "research"): OWN})
        configure(ctx.paths, agents=[{"name": "research", "harness": "codex"}, {"name": "g1", "harness": "grok"}]
                  + [{"name": n, "harness": "claude"} for n in self.NAMES])
        return ctx, fake

    def test_every_claude_agent_uses_its_own_token_and_loses_only_the_account_connector(self):
        ctx, _ = self.ctx()
        for name in self.NAMES:
            with self.subTest(name=name):
                seen = start(ctx, name, ["Auftrag"])
                self.assertEqual(seen["env"]["MESH_MOSHI_TOKEN"], OWN)
                self.assertNotIn(OWN, " ".join(seen["argv"]))  # in the environment only
                config_at = seen["argv"].index("--mcp-config")
                server = json.loads(seen["argv"][config_at + 1])["mcpServers"]["moshi"]
                self.assertEqual((server["url"], server["headers"]["Authorization"]),
                                 (MOSHI, "Bearer ${MESH_MOSHI_TOKEN}"))
                # --mcp-config is variadic: the next argument has to be an option, or it swallows the task
                self.assertTrue(seen["argv"][config_at + 2].startswith("--"))
                settings = json.loads(seen["argv"][seen["argv"].index("--settings") + 1])["permissions"]
                self.assertEqual(settings["deny"], ["mcp__claude_ai_moshi*", "mcp__claude_ai_Moshi*"])
                self.assertIn("mcp__moshi__mesh_send", settings["allow"])
                self.assertNotIn("mcp__moshi__mesh_webhook_set", settings["allow"])  # still asks
                self.assertEqual(seen["argv"][-1], "Auftrag")
                self.assertNotIn("--disallowedTools", seen["argv"])

    def test_claude_gets_its_rules_with_its_own_moshi_name(self):
        ctx, _ = self.ctx()
        seen = start(ctx, "writer")
        rules = seen["argv"][seen["argv"].index("--append-system-prompt") + 1]
        self.assertIn("`writer`", rules)
        self.assertIn("mesh_register", rules)

    def test_no_token_no_start(self):
        ctx, _ = context(self)
        configure(ctx.paths, agents=[{"name": "writer", "harness": "claude"}])
        with self.assertRaises(ValueError) as caught:
            start(ctx, "writer")
        self.assertIn("moshi-cockpit bot add writer", str(caught.exception))

    def test_without_moshi_claude_starts_without_the_wiring(self):
        ctx, _ = context(self)
        configure(ctx.paths, servers={}, agents=[{"name": "writer", "harness": "claude"}])
        seen = start(ctx, "writer")
        self.assertNotIn("--mcp-config", seen["argv"])
        self.assertIn("--settings", seen["argv"])

    def test_grok_stays_without_a_token_and_without_the_claude_wiring(self):
        ctx, _ = self.ctx()
        seen = start(ctx, "g1", ["Auftrag"])
        self.assertNotIn("MESH_MOSHI_TOKEN", seen["env"])
        self.assertNotIn("--mcp-config", seen["argv"])


class SingleAgyTest(unittest.TestCase):
    def test_a_second_agy_agent_is_refused_everywhere(self):
        ctx, _ = context(self)
        configure(ctx.paths, agents=[{"name": "ag1", "harness": "agy"}])
        with self.assertRaises(ValueError):
            registry.add(ctx.paths, ctx.backup, registry.Agent(name="ag2", harness="agy"))
        configure(ctx.paths, agents=[{"name": "ag1", "harness": "agy"}, {"name": "ag2", "harness": "agy"}])
        with self.assertRaises(ValueError):
            registry.load_agents(ctx.paths)


class SingleInstanceRepairTest(unittest.TestCase):  # one-agent rule (agy): refused with reason, repairable
    def test_a_second_agy_agent_is_refused_on_every_path_with_the_reason(self):
        from cockpit import cli, wizard
        ctx, _ = context(self)
        configure(ctx.paths, agents=[{"name": "a1", "harness": "agy"}])
        with self.assertRaises(ValueError) as caught:
            registry.add(ctx.paths, ctx.backup, registry.Agent(name="a2", harness="agy"))
        self.assertIn("globale Config", str(caught.exception))
        with self.assertRaises(ValueError) as caught:  # the panel's "+ Agent" and agent add take this path
            wizard.create(ctx, "a2", "agy")
        self.assertIn("'a1'", str(caught.exception))
        offer = {h["name"]: h for h in wizard.harness_offer(ctx.paths)}
        self.assertFalse(offer["agy"]["available"])
        with mock.patch("cockpit.cli.Paths.from_env", return_value=ctx.paths), redirect_stderr(io.StringIO()) as err:
            self.assertEqual(cli.main(["agent", "add", "a2", "--harness=agy"]), 1)
        self.assertIn("globale Config", err.getvalue())
        # a list that already holds two (written by hand or before this change) stops every start
        configure(ctx.paths, agents=[{"name": "a1", "harness": "agy"}, {"name": "a2", "harness": "agy"}])
        with self.assertRaises(ValueError):
            launch.command(ctx.paths, ctx.keychain, "a1", [])

    def test_a_list_with_two_agy_agents_can_be_shown_and_repaired(self):
        # The rule blocks creating and starting, never clean-up or display.
        from cockpit import cli, status, wizard
        ctx, _ = context(self)
        configure(ctx.paths, servers={}, agents=[{"name": "a1", "harness": "agy"}, {"name": "a2", "harness": "agy"},
                                                 {"name": "c1", "harness": "codex"}])
        with mock.patch("cockpit.cli.Paths.from_env", return_value=ctx.paths), \
                mock.patch("cockpit.cli.default_store", return_value=ctx.keychain):  # no `security` on CI images
            with redirect_stdout(io.StringIO()) as out:
                self.assertEqual(cli.main(["status", "--json"]), 0)
            data = json.loads(out.getvalue())
            self.assertEqual([a["name"] for a in data["local"]], ["a1", "a2", "c1"])
            self.assertIn("agent remove a2", data["problems"][0])
            with redirect_stdout(io.StringIO()) as out:
                self.assertEqual(cli.main(["status"]), 0)
            self.assertIn("Note: agy", out.getvalue())
            with redirect_stdout(io.StringIO()) as out:
                self.assertEqual(cli.main(["agent", "list"]), 0)
            self.assertIn("agent remove a2", out.getvalue())
            for name in ("a1", "c1"):  # every start is refused while the list breaks the rule
                with self.subTest(start=name), self.assertRaises(ValueError) as caught:
                    launch.command(ctx.paths, ctx.keychain, name, [])
                self.assertIn("agent remove", str(caught.exception))
            self.assertFalse({h["name"]: h for h in wizard.harness_offer(ctx.paths)}["agy"]["available"])
            report = {c.name: c for c in doctor.check_agents(doctor.DoctorContext(paths=ctx.paths,
                                                                                   keychain=ctx.keychain))}
            self.assertEqual(report["agents"].status, doctor.FAIL)
            with redirect_stdout(io.StringIO()):
                self.assertEqual(cli.main(["agent", "remove", "a2"]), 0)  # the way out works
        self.assertEqual([a.name for a in registry.load_agents(ctx.paths)], ["a1", "c1"])  # strict again
        self.assertEqual(launch.command(ctx.paths, ctx.keychain, "a1", [])[1][:1], ["agy"])
        self.assertEqual(status.problems(ctx.paths), [])

    def test_one_agy_still_starts_and_grok_has_no_limit(self):
        ctx, _ = context(self)
        configure(ctx.paths, servers={}, agents=[{"name": "a1", "harness": "agy"}, {"name": "c1", "harness": "codex"},
                                                 {"name": "g1", "harness": "grok"}, {"name": "g2", "harness": "grok"}])
        self.assertEqual(launch.command(ctx.paths, ctx.keychain, "a1", [])[1][:1], ["agy"])
        # grok has no moshi of its own locally (only grok.ai has), so several grok agents are fine
        self.assertEqual(launch.command(ctx.paths, ctx.keychain, "g2", [])[1][:1], ["grok"])


class NameTest(unittest.TestCase):
    def test_one_rule_for_names_no_paths_spaces_or_lookalikes(self):
        for bad in ("../x", "a/b", "a b", " a", "аgent", "reѕearch", "a\nb", "", "-a", "x" * 65, "Cockpit"):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                registry.validate_name(bad)
        ctx, _ = context(self)
        registry.add(ctx.paths, ctx.backup, registry.Agent(name="Research", harness="codex"))
        with self.assertRaises(ValueError):
            registry.add(ctx.paths, ctx.backup, registry.Agent(name="research", harness="vibe"))


class MigrationTest(unittest.TestCase):
    def test_twice_is_once_without_keychain_writes_and_with_backups_first(self):
        ctx, fake = context(self, {("moshi-vibe", "vibe"): OWN, ("ocellio-vibe", "vibe"): OWN,
                                   ("moshi-cockpit", "cockpit"): OWN})
        ctx.paths.agy_mcp_config.parent.mkdir(parents=True)
        ctx.paths.agy_mcp_config.write_text(json.dumps({"mcpServers": {"moshi": {"serverUrl": MOSHI},
                                                                       "ocellio": {"serverUrl": OCELLIO}}}))
        install(ctx)
        first = (ctx.paths.agents_file.read_text(), ctx.paths.mesh_config.read_text())
        install(ctx)
        self.assertEqual((ctx.paths.agents_file.read_text(), ctx.paths.mesh_config.read_text()), first)
        writes = [args for args, _ in fake.calls if args[:2] in (["security", "-i"], ["security", "delete-generic-password"])]
        self.assertEqual(writes, [])
        backed_up = {str(e.get("path")) for e in ctx.backup.entries()}
        self.assertIn(str(ctx.paths.agents_file), backed_up)
        self.assertIn(str(ctx.paths.mesh_config), backed_up)
        self.assertEqual(registry.find(ctx.paths, "vibe").bound, {"moshi": "https://moshi.example.test",
                                                                   "ocellio": "https://ocellio.example.test"})


class MigrationSafetyTest(unittest.TestCase):
    def _legacy(self, current_moshi=None):
        ctx, fake = context(self, {("moshi-vibe", "vibe"): OWN, ("moshi-cockpit", "cockpit"): OWN})
        ctx.paths.agy_mcp_config.parent.mkdir(parents=True)
        ctx.paths.agy_mcp_config.write_text(json.dumps({"mcpServers": {"moshi": {"serverUrl": MOSHI}}}))
        if current_moshi:
            configure(ctx.paths, servers={"moshi": current_moshi})
            ctx.paths.agents_file.unlink()
        return ctx

    def test_it_runs_once_ever_even_if_the_agent_list_is_deleted(self):
        ctx = self._legacy()
        install(ctx)
        ctx.paths.agents_file.unlink()
        notes = install(ctx)
        self.assertFalse(any("Migration" in n for n in notes))
        self.assertFalse(ctx.paths.agents_file.exists())

    def test_an_existing_agent_list_stamps_migrated_too(self):
        ctx = self._legacy()
        configure(ctx.paths, servers={}, agents=[{"name": "own", "harness": "codex"}])
        notes = install(ctx)
        self.assertFalse(any("Migration" in n for n in notes))
        self.assertTrue(json.loads(ctx.paths.mesh_config.read_text()).get("migrated"))
        ctx.paths.agents_file.unlink()
        again = install(ctx)
        self.assertFalse(any("Migration" in n for n in again))
        self.assertFalse(ctx.paths.agents_file.exists())

    def test_a_config_pointing_elsewhere_leaves_tokens_unbound(self):
        ctx = self._legacy(current_moshi="https://evil.example/mcp")
        notes = install(ctx)
        self.assertTrue(any("unbound" in n for n in notes), notes)
        self.assertEqual(registry.find(ctx.paths, "vibe").bound, {})
        self.assertNotIn("bound", json.loads(ctx.paths.mesh_config.read_text()))  # no helper identity bound either
        with self.assertRaises(ValueError):
            start(ctx, "vibe")


class NoNetworkTest(unittest.TestCase):
    def test_without_servers_nothing_connects(self):
        paths = temp_paths(self)
        attempts = []

        def refuse(*args, **_kw):
            attempts.append(args)
            raise OSError("network is not allowed in this test")
        env = {"HOME": str(paths.home), "PATH": os.environ.get("PATH", "")}
        from cockpit import cli
        with mock.patch.dict(os.environ, env, clear=True), \
                mock.patch.object(socket.socket, "connect", refuse), \
                mock.patch.object(socket, "create_connection", refuse), \
                mock.patch("cockpit.secrets.macos.MacKeychain.__init__", lambda self, *a, **k: None), \
                mock.patch("cockpit.secrets.macos.MacKeychain.exists", lambda *a, **k: False), \
                redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
            self.assertEqual(cli.main(["agent", "add", "solo", "--harness", "vibe"]), 0)
            self.assertEqual(cli.main(["status", "--json"]), 0)
            self.assertEqual(cli.main(["agent", "list", "--json"]), 0)
            cli.main(["doctor", "--json"])
            launch.command(paths, MacKeychain(), "solo", ["hi"])
        self.assertEqual(attempts, [])


class OwnRulesTest(unittest.TestCase):  # the person's data rule must persist
    RULE = "No customer or patient data (context example-lab)."

    def setUp(self):
        self.ctx, _ = context(self, {(f"{s}-{n}", n): OWN for n in ("vibe", "agy", "codex") for s in ("moshi", "ocellio")})
        configure(self.ctx.paths, agents=[{"name": "vibe", "harness": "vibe"}, {"name": "agy", "harness": "agy"},
                                          {"name": "codex", "harness": "codex"}])
        config.save_rules(self.ctx.paths, self.ctx.backup, self.RULE + "\x1b[2J\n")

    def test_every_harness_carries_the_rules(self):
        from cockpit.harnesses import HARNESSES, Ctx
        for name in ("vibe", "agy"):
            agents.setup(self.ctx, registry.find(self.ctx.paths, name))
        vibe_md = (self.ctx.paths.agent_home("vibe") / "vibe" / "AGENTS.md").read_text()
        agy_md = self.ctx.paths.agy_agents_md.read_text()
        hctx = Ctx(paths=self.ctx.paths, config=config.load_config(self.ctx.paths), keychain=self.ctx.keychain)
        codex = " ".join(HARNESSES["codex"].args(hctx, registry.find(self.ctx.paths, "codex")))
        for text in (vibe_md, agy_md, codex):
            self.assertIn(self.RULE, text)
            self.assertNotIn("\x1b", text)

    def test_install_never_overwrites_them_and_keeps_them_in_the_agents(self):
        before = self.ctx.paths.rules_file.read_text()
        install(self.ctx)
        install(self.ctx)
        self.assertEqual(self.ctx.paths.rules_file.read_text(), before)
        self.assertIn(self.RULE, self.ctx.paths.agy_agents_md.read_text())

    def test_doctor_names_agents_the_rules_do_not_reach(self):
        configure(self.ctx.paths, agents=[{"name": "vibe", "harness": "vibe"}, {"name": "gr", "harness": "grok"}])
        config.save_rules(self.ctx.paths, self.ctx.backup, self.RULE)
        dctx = doctor.DoctorContext(paths=self.ctx.paths, keychain=self.ctx.keychain)
        rules = doctor.check_rules(dctx)[0]
        self.assertEqual(rules.status, doctor.WARN)
        self.assertIn("gr", rules.detail)
        config.save_rules(self.ctx.paths, self.ctx.backup, "")
        self.assertEqual(doctor.check_rules(dctx), [])

    def test_rules_are_owner_only_and_bounded(self):
        os.chmod(self.ctx.paths.rules_file, 0o644)
        with self.assertRaises(ValueError):
            config.load_config(self.ctx.paths)
        os.chmod(self.ctx.paths.rules_file, 0o600)
        with self.assertRaises(ValueError):
            config.save_rules(self.ctx.paths, self.ctx.backup, "x" * (config.MAX_RULES + 1))
        config.save_rules(self.ctx.paths, self.ctx.backup, "")
        self.assertEqual(config.load_config(self.ctx.paths).rules, "")


class MoshiNameTest(unittest.TestCase):
    def test_an_agents_moshi_name_equals_its_local_name(self):
        from cockpit import status
        from cockpit.harnesses import HARNESSES, Ctx
        ctx, _ = context(self)
        configure(ctx.paths, agents=[{"name": "research", "harness": "codex"}])
        cfg = config.load_config(ctx.paths)
        agent = registry.find(ctx.paths, "research")
        self.assertEqual(HARNESSES["codex"].moshi_name(Ctx(paths=ctx.paths, config=cfg, keychain=ctx.keychain), agent),
                         "research")
        self.assertEqual(status.local_agents(ctx.paths, cfg, ctx.keychain)[0]["moshi_name"], "research")

    def test_the_config_has_no_person(self):
        ctx, _ = context(self)
        configure(ctx.paths, agents=[])
        self.assertFalse(hasattr(config.load_config(ctx.paths), "person"))
        ctx.paths.mesh_config.write_text(json.dumps({"person": "anna", "servers": {"moshi": MOSHI}}))
        self.assertEqual(config.load_config(ctx.paths).servers, {"moshi": MOSHI})  # a leftover key is ignored


if __name__ == "__main__":
    unittest.main()
