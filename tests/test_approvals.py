import json
import os
import re
import unittest
from unittest import mock

from cockpit import approvals, launch, policy
from cockpit.agents import AgentContext
from cockpit.config import Config
from cockpit.presence import presence_text
from cockpit.backup import BackupStore
from cockpit.secrets.macos import MacKeychain
from cockpit.paths import BIN
from tests.helpers import MOSHI, OCELLIO, FakeSecurity, configure, temp_paths
from tests.test_leak_check import leak_check


def ctx_for(test, entries=None):
    paths = temp_paths(test)
    return AgentContext(paths=paths, backup=BackupStore(paths.state), keychain=MacKeychain(FakeSecurity(entries or {})))


class PresenceTest(unittest.TestCase):
    def test_presence_is_neutral_and_follows_the_servers(self):
        both = presence_text("codex", Config(servers={"moshi": MOSHI, "ocellio": OCELLIO}), "Review")
        self.assertIn("`codex`", both)
        self.assertIn("Rolle: Review", both)
        self.assertIn("mesh_register", both)
        self.assertIn("context_load", both)
        for tool in policy.IRREVERSIBLE:
            self.assertIn(f"`{tool}`", both)
        self.assertIn("IRREVERSIBEL:", both)
        with_skill = presence_text("x", Config(servers={"ocellio": OCELLIO}), "Review", "skill-a")
        self.assertIn("`skill-a` mit `load_skill`", with_skill)
        self.assertIn("in deiner ersten Antwort", with_skill)
        self.assertNotIn("load_skill", presence_text("x", Config(servers={"moshi": MOSHI}), "", "skill-a"))
        self.assertNotIn("load_skill", both)
        alone = presence_text("solo", Config())
        self.assertNotIn("moshi", alone)
        self.assertNotIn("ocellio", alone)
        # No person, customer, host or internal id: the repo's leak guard agrees. The private word list is used
        # where it exists; without it the other rules still apply ("-" never matches a token).
        words = leak_check.load_words(os.environ) or frozenset({"-"})
        for line in (both + alone).splitlines():
            self.assertEqual(leak_check.scan_line(line, words), [], line)


class ApprovalTest(unittest.TestCase):
    def test_agy_rules_that_answer_ask_tools(self):
        for rule in ("mcp", "mcp(*)", "mcp(ocellio)", "mcp(ocellio/*)", "mcp(ocellio/update_entity)",
                     "mcp(moshi/mesh_webhook_set)", "mcp(*/archive_entity)", "mcp(ocellio/upd*)"):
            with self.subTest(rule=rule):
                self.assertTrue(approvals.agy_rule_grants_ask(rule))
        for rule in ("mcp(ocel*/update_entity)", "mcp(*ellio/*)", "mcp(ocellio/u*_entity)", "MCP(OCELLIO/Update_Entity)",
                     "mcp(?oshi/mesh_webhook_*)", "mcp(ocellio/[ua]*)"):
            with self.subTest(glob=rule):
                self.assertTrue(approvals.agy_rule_grants_ask(rule))
        for rule in ("mcp(ocellio/get_entity)", "mcp(moshi/mesh_send)", "mcp(other/update_entity)", "bash(ls)",
                     "mcp(ocellio/get_*)", "mcp(moshi/mesh_s*)"):
            with self.subTest(rule=rule):
                self.assertFalse(approvals.agy_rule_grants_ask(rule))

    def test_agy_settings_that_switch_asking_off(self):
        ctx = ctx_for(self)
        ctx.paths.agy_settings.parent.mkdir(parents=True)
        ctx.paths.agy_settings.write_text(json.dumps({"permissions": {"allow": ["mcp(ocellio/archive_entity)"]},
                                                      "dangerouslySkipPermissions": True, "yolo": False,
                                                      "approvalMode": "yolo", "tools": {"permissionMode": "auto"},
                                                      "theme": "auto"}))
        findings = approvals.agy_findings(ctx.paths)
        self.assertTrue(any("archive_entity" in f for f in findings))
        self.assertTrue(any("dangerouslySkipPermissions" in f for f in findings))
        self.assertTrue(any("approvalMode" in f for f in findings))
        self.assertTrue(any("tools.permissionMode" in f for f in findings))
        self.assertFalse(any("'yolo'" in f or "theme" in f for f in findings))

    def test_findings_name_the_setting_never_its_value(self):
        ctx = ctx_for(self, {("ocellio-agy", "agy"): "oc_AGY_token_12345678", ("moshi-agy", "agy"): "bt_AGY_token_12345678"})
        configure(ctx.paths, agents=[{"name": "agy", "harness": "agy"}])
        ctx.paths.agy_settings.parent.mkdir(parents=True)
        ctx.paths.agy_settings.write_text(json.dumps({"apiKey": "secret_yolo_123", "tokens": ["bt_bypass_456789"],
                                                      "approvalMode": "always"}))
        out = " | ".join(approvals.agy_findings(ctx.paths))
        self.assertIn("apiKey", out)
        for value in ("secret_yolo_123", "bt_bypass_456789", "always"):
            self.assertNotIn(value, out)
        from cockpit import doctor
        dctx = doctor.DoctorContext(paths=ctx.paths, keychain=ctx.keychain, probe=lambda _u: (401, "Bearer"),
                                    herdr_versions=lambda _p: (None, None))
        for as_json in (False, True):
            rendered = doctor.render(doctor.run(dctx), as_json=as_json)
            self.assertIn("apiKey", rendered)  # the agy check really ran and reported the setting
            for value in ("secret_yolo_123", "bt_bypass_456789"):
                self.assertNotIn(value, rendered)

    def test_secret_looking_keys_are_masked_in_the_path(self):
        ctx = ctx_for(self)
        ctx.paths.agy_settings.parent.mkdir(parents=True)
        ctx.paths.agy_settings.write_text(json.dumps({
            "bt_bypass_456789": True, "x9F2kQ7p": {"approvalMode": "yolo"},
            "dangerouslySkipPermissions": True, "tools": {"auto-approve": True}}))
        out = " | ".join(approvals.agy_findings(ctx.paths))
        for secret in ("bt_bypass_456789", "x9F2kQ7p"):
            self.assertNotIn(secret, out)
        self.assertIn(approvals.MASKED_KEY, out)
        self.assertIn(f"{approvals.MASKED_KEY}.approvalMode", out)  # parent masked, setting still named
        self.assertIn("dangerouslySkipPermissions", out)
        self.assertIn("tools.auto-approve", out)

    def test_key_check_stays_linear_on_long_keys(self):
        import time
        ctx = ctx_for(self)
        ctx.paths.agy_settings.parent.mkdir(parents=True)
        keys = ["a" * 47 + "1", "ab_" * 15 + "1", "a" * 30 + "-" + "b" * 16 + "9"]
        ctx.paths.agy_settings.write_text(json.dumps({k: True for k in keys} | {"approvalModes": {k: "yolo" for k in keys}}))
        started = time.monotonic()
        out = " | ".join(approvals.agy_findings(ctx.paths))
        self.assertLess(time.monotonic() - started, 0.5)
        for key in keys:
            self.assertNotIn(key, out)
        self.assertIn(f"approvalModes.{approvals.MASKED_KEY}", out)
        for plain in ("approvalMode", "auto-approve", "snake_case_key", "a" * 48):
            self.assertEqual(approvals._shown_key(plain), plain)
        for hidden in ("bt_bypass_456789", "x9F2kQ7p", "a__b", "-a", "a" * 49):
            self.assertEqual(approvals._shown_key(hidden), approvals.MASKED_KEY)

    def test_bare_strings_in_lists_are_checked_too(self):
        ctx = ctx_for(self)
        ctx.paths.agy_settings.parent.mkdir(parents=True)
        ctx.paths.agy_settings.write_text(json.dumps({
            "approvalModes": ["yolo"], "permissionModes": ["default", "always"], "nested": [["bypassPermissions"]],
            "themes": ["auto", "dark"], "permissions": {"allow": ["mcp(ocellio/get_entity)"]}}))
        findings = " | ".join(approvals.agy_findings(ctx.paths))
        self.assertIn("approvalModes[0]", findings)
        self.assertIn("permissionModes[1]", findings)
        self.assertIn("nested[0][0]", findings)
        self.assertNotIn("themes", findings)
        self.assertNotIn("get_entity", findings)

    def test_grok_always_approve_is_a_finding(self):
        ctx = ctx_for(self)
        self.assertEqual(approvals.grok_findings(ctx.paths), [])  # no grok config: nothing to say
        ctx.paths.grok_config.parent.mkdir(parents=True)
        ctx.paths.grok_config.write_text('yolo = false\npermission_mode = "always-approve"\n')
        self.assertTrue(any("ein direkter grok-Start nicht" in f for f in approvals.grok_findings(ctx.paths)))
        ctx.paths.grok_config.write_text("permission_mode = 'always-approve'\n")
        self.assertEqual(len(approvals.grok_findings(ctx.paths)), 1)
        ctx.paths.grok_config.write_text('yolo = true\npermission_mode = "default"\n')
        self.assertEqual(len(approvals.grok_findings(ctx.paths)), 1)
        ctx.paths.grok_config.write_text('permission_mode = "default"\n')
        self.assertEqual(approvals.grok_findings(ctx.paths), [])

    def test_codex_asks_for_every_ask_tool_and_no_model_reviews(self):
        args = approvals.codex_args("Zeile 1\n\"zitiert\"", {"moshi": MOSHI, "ocellio": OCELLIO},
                                    {"moshi": "MESH_MOSHI_TOKEN", "ocellio": "MESH_OCELLIO_TOKEN"})
        flat = " ".join(args)
        self.assertIn('approvals_reviewer="user"', flat)
        for tool in policy.OCELLIO_ASK:
            self.assertIn(f'mcp_servers.ocellio.tools.{tool}.approval_mode="prompt"', flat)
        for tool in policy.OCELLIO_ALWAYS:
            self.assertIn(f'mcp_servers.ocellio.tools.{tool}.approval_mode="approve"', flat)
        self.assertIn('mcp_servers.moshi.default_tools_approval_mode="prompt"', flat)
        self.assertIn(f"mcp_servers.moshi.url={json.dumps(MOSHI)}", flat)
        none = " ".join(approvals.codex_args("x", {}, {}))
        self.assertNotIn("mcp_servers", none)
        instructions = next(a for a in args if a.startswith("developer_instructions="))
        self.assertEqual(json.loads(instructions.split("=", 1)[1]), "Zeile 1\n\"zitiert\"")

    def test_run_keeps_tokens_out_of_argv_and_cleans_the_environment(self):
        ctx = ctx_for(self, {("ocellio-research", "research"): "oc_SECRET_value_1234567",
                             ("moshi-research", "research"): "bt_SECRET_value_1234567"})
        configure(ctx.paths, agents=[{"name": "research", "harness": "codex"}])
        seen = {}
        environ = {"PATH": "/usr/bin", "HC_TERMINAL_SOCKET": "/x", "SSH_AUTH_SOCK": "/y", "HERDR_X": "1"}
        with mock.patch.object(launch.shutil, "which", return_value="/usr/bin/true"):
            launch.run_agent(ctx.paths, ctx.keychain, "research", ["exec", "hi"],
                             execve=lambda path, argv, env: seen.update(argv=argv, env=env), environ=environ)
        self.assertNotIn("SECRET", " ".join(seen["argv"]))
        self.assertEqual(seen["argv"][0], "codex")
        self.assertEqual(seen["argv"][-2:], ["exec", "hi"])
        self.assertEqual(seen["env"]["MESH_MOSHI_TOKEN"], "bt_SECRET_value_1234567")
        for dropped in ("HC_TERMINAL_SOCKET", "SSH_AUTH_SOCK", "HERDR_X"):
            self.assertNotIn(dropped, seen["env"])

    def test_run_per_harness_without_servers(self):
        from cockpit import launch as l
        ctx = ctx_for(self)
        configure(ctx.paths, servers={}, agents=[{"name": n, "harness": n} for n in ("claude", "codex", "vibe", "agy", "grok")])
        for name in ("claude", "codex", "vibe", "agy", "grok"):
            harness, argv, env = l.command(ctx.paths, ctx.keychain, name, ["Auftrag"])
            with self.subTest(name=name):
                self.assertEqual(argv[0], harness.binary)
                self.assertFalse([k for k in env if k.startswith("MESH_")])  # no servers, no tokens
                self.assertNotIn("mcp_servers", " ".join(argv))
        self.assertEqual(l.command(ctx.paths, ctx.keychain, "agy", ["Auftrag"])[1][-2:], ["-i", "Auftrag"])
        self.assertIn("manual", l.command(ctx.paths, ctx.keychain, "claude", [])[1])
        self.assertIn("default", l.command(ctx.paths, ctx.keychain, "grok", [])[1])

    def test_no_launcher_carries_an_auto_approve_flag(self):
        flags = re.compile(r"--(auto-approve|always-approve|dangerously|yolo)|bypassPermissions|full-auto")
        for launcher in ("moshi-cockpit",):
            with self.subTest(launcher=launcher):
                self.assertIsNone(flags.search((BIN / launcher).read_text()))


if __name__ == "__main__":
    unittest.main()
