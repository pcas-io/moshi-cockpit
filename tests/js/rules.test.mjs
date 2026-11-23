// Parity of the dialog's checks with cockpit/registry.py. Run: node --test tests/js/
import { test } from "node:test";
import assert from "node:assert/strict";
import { addArgs, checkName, checkSkill, loadArgs, removeArgs, roleText, runArgv, setArgs, whereArgs } from "../../share/herdr-mods/mesh-agents/rules.js";
import { ownAgents, runningTerminal } from "../../share/herdr-mods/moshi/list.js";

test("names follow moshi's rule and the reserved list", () => {
  assert.equal(checkName("recherche", []), null);
  assert.equal(checkName("anna-research_2", []), null);
  for (const bad of ["", "-x", "_x", "a b", "../x", "ä", "x".repeat(65), "--harness=claude"]) {
    assert.notEqual(checkName(bad, []), null, bad);
  }
  // device and default name the installation's own Keychain entries (moshi-device, ocellio-default)
  for (const reserved of ["admin", "Broadcast", "COCKPIT", "register", "device", "Default"]) {
    assert.match(checkName(reserved, []), /reserved/);
  }
  assert.match(checkName("Codex", [{ name: "codex" }]), /exists already/);
});

test("roles lose control characters and are bounded like registry._validate", () => {
  assert.equal(roleText("  Research \u001b[31m and\tsearch\n "), "Research [31m and search");
  assert.equal(roleText("\u0000"), "");
  assert.equal(roleText("x".repeat(200)).length, 64);
  // UTF-16 units, like moshi's `.length`: an emoji counts 2 and is never cut in half
  assert.equal(roleText("x".repeat(63) + "😀"), "x".repeat(63));
  assert.equal(roleText("x".repeat(62) + "😀😀"), "x".repeat(62) + "😀");
  assert.equal(roleText("😀".repeat(40)).length, 64);
});

test("roles follow remote.check_role: what it refuses, roleText cleans", () => {
  // the same samples as tests/test_remote.py test_role_rule_matches_the_dialog: raw → what the dialog sends
  const samples = [["dev\u200dops", "dev ops"], ["a\u202eb", "a b"], ["a\u2028b", "a b"], ["\ue000x", "x"],
                   ["soft\u00adhyphen", "soft hyphen"], ["x".repeat(65), "x".repeat(64)],
                   ["😀".repeat(33), "😀".repeat(32)], ["Code review", "Code review"]];
  for (const [raw, sent] of samples) assert.equal(roleText(raw), sent, JSON.stringify(raw));
});

test("arguments never let a value become an option", () => {
  assert.deepEqual(addArgs("a", "codex", ""), ["agent", "add", "a", "--harness=codex"]);
  assert.deepEqual(addArgs("a", "codex", "--rebind"), ["agent", "add", "a", "--harness=codex", "--role=--rebind"]);
  assert.deepEqual(removeArgs("a"), ["agent", "remove", "a"]);
  assert.throws(() => removeArgs("--help"));
  assert.deepEqual(loadArgs("scout"), ["agent", "load", "scout"]);
  assert.throws(() => loadArgs("--take-over"));
  assert.deepEqual(whereArgs("scout"), ["agent", "where", "scout", "--json"]);
  assert.throws(() => whereArgs("-x"));
  // --take-over right after the name is the CLI's; everything later goes to the harness
  assert.deepEqual(runArgv("/c", "scout", false, ["resume", "1"]), ["/c", "run", "scout", "resume", "1"]);
  assert.deepEqual(runArgv("/c", "scout", true, ["resume", "1"]), ["/c", "run", "scout", "--take-over", "resume", "1"]);
  assert.throws(() => runArgv("/c", "--take-over", false));
});

test("skill names follow the agent name rule and never become options", () => {
  assert.equal(checkSkill(""), null);
  assert.equal(checkSkill("skill-a"), null);
  for (const bad of ["a b", "-x", "../x", "a`b"]) assert.notEqual(checkSkill(bad), null, bad);
  assert.deepEqual(addArgs("a", "codex", "", "skill-b"), ["agent", "add", "a", "--harness=codex", "--skill=skill-b"]);
  assert.deepEqual(setArgs("a", "", ""), ["agent", "set", "a", "--role=", "--skill="]);
  assert.throws(() => setArgs("--x", "", ""));
});

test("own agents only, presence only with an own moshi identity", () => {
  const local = [{ name: "research", harness: "vibe", has_moshi_token: true, installed: true },
                 { name: "grok", harness: "grok", has_moshi_token: false, installed: true }];
  const mesh = [{ name: "Grok", presence: "live", working_on: "x" }, { name: "RESEARCH", presence: "stale", working_on: "y" },
                { name: "someone-else", presence: "live" }];
  const list = ownAgents(local, mesh);
  assert.deepEqual(list.map((a) => [a.name, a.presence]), [["research", "stale"], ["grok", null]]);
  assert.equal(list[1].working_on, "");
  assert.deepEqual(ownAgents(undefined, undefined), []);
});

test("a running agent is found by its tab, never by its CLI alone", () => {
  const t = (tab_name, address, seq, terminal = "x") => ({ tab_name, address, seq, terminal });
  assert.equal(runningTerminal("marv", [t("marv", "a", 1), t("marv", "b", 2)]), "b");
  assert.equal(runningTerminal("grok", [t("grok (no moshi identity of its own)", "a", 1)]), "a");
  assert.equal(runningTerminal("codex", [t("Codex", "a", 1), t("codex2", "b", 1), t("codex", "c", 1, null)]), null);
});
