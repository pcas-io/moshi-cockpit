// Parity of the dialog's checks with cockpit/registry.py. Run: node --test tests/js/
import { test } from "node:test";
import assert from "node:assert/strict";
import { addArgs, checkName, checkSkill, removeArgs, roleText, setArgs } from "../../share/herdr-mods/mesh-agents/rules.js";
import { ownAgents, runningTerminal } from "../../share/herdr-mods/moshi/list.js";

test("names follow moshi's rule and the reserved list", () => {
  assert.equal(checkName("recherche", []), null);
  assert.equal(checkName("anna-research_2", []), null);
  for (const bad of ["", "-x", "_x", "a b", "../x", "ä", "x".repeat(65), "--harness=claude"]) {
    assert.notEqual(checkName(bad, []), null, bad);
  }
  for (const reserved of ["admin", "Broadcast", "COCKPIT", "register"]) {
    assert.match(checkName(reserved, []), /reserviert/);
  }
  assert.match(checkName("Codex", [{ name: "codex" }]), /schon/);
});

test("roles lose control characters and are bounded like registry._validate", () => {
  assert.equal(roleText("  Recherche \u001b[31m und\tSuche\n "), "Recherche [31m und Suche");
  assert.equal(roleText("\u0000"), "");
  assert.equal(roleText("x".repeat(200)).length, 120);
  const cut = roleText("x".repeat(119) + "😀😀");  // code points, like Python: no lone surrogate
  assert.equal(Array.from(cut).length, 120);
  assert.ok(cut.endsWith("😀"));
});

test("arguments never let a value become an option", () => {
  assert.deepEqual(addArgs("a", "codex", ""), ["agent", "add", "a", "--harness=codex"]);
  assert.deepEqual(addArgs("a", "codex", "", true), ["agent", "add", "a", "--harness=codex", "--connect"]);
  assert.deepEqual(addArgs("a", "codex", "--rebind"), ["agent", "add", "a", "--harness=codex", "--role=--rebind"]);
  assert.deepEqual(removeArgs("a"), ["agent", "remove", "a", "--keep-moshi"]);
  assert.deepEqual(removeArgs("a", true), ["agent", "remove", "a", "--revoke-moshi"]);
  assert.throws(() => removeArgs("--help"));
});

test("skill names follow the agent name rule and never become options", () => {
  assert.equal(checkSkill(""), null);
  assert.equal(checkSkill("skill-a"), null);
  for (const bad of ["a b", "-x", "../x", "a`b"]) assert.notEqual(checkSkill(bad), null, bad);
  assert.deepEqual(addArgs("a", "codex", "", true, "skill-b"), ["agent", "add", "a", "--harness=codex", "--skill=skill-b", "--connect"]);
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
  assert.equal(runningTerminal("grok", [t("grok (kein eigenes moshi)", "a", 1)]), "a");
  assert.equal(runningTerminal("codex", [t("Codex", "a", 1), t("codex2", "b", 1), t("codex", "c", 1, null)]), null);
});
