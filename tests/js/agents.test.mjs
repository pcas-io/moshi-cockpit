// The "+ Agent" dialog, the takeover question and the sidebar's connection line. Run: node --test tests/js/
import { test } from "node:test";
import assert from "node:assert/strict";
import { HERDR_APP, harnessRows } from "../../share/herdr-mods/mesh-agents/harnesses.js";
import { catalogRows, settle } from "../../share/herdr-mods/mesh-agents/catalog.js";
import { takeoverFrom } from "../../share/herdr-mods/mesh-agents/rules.js";
import { sidebarLine } from "../../share/herdr-mods/moshi/list.js";

const CLI = "/Users/x/.local/bin/moshi-cockpit";
const offer = (name, installed, available = installed, reason = "") => ({ name, installed, available, reason, native_mesh: true });

test("harness map marks missing harnesses with an install action", () => {
  // herdr's program keys as knownAgents() names them on a real machine (agy is "antigravity")
  assert.deepEqual(HERDR_APP, { claude: "claude", codex: "codex", vibe: "vibe", agy: "antigravity", grok: "grok" });
  const known = [{ key: "codex", name: "Codex", installed: true }, { key: "antigravity", name: "Antigravity", installed: false },
                 { key: "claude", name: "Claude Code", installed: true }, { key: "pi", name: "Pi", installed: true }];
  const rows = harnessRows([offer("claude", false), offer("codex", false), offer("vibe", true),
                            offer("agy", false, false, "not installed (agy)"),
                            offer("grok", true, false, "connects through your account")], known, CLI);
  const by = Object.fromEntries(rows.map((r) => [r.name, r]));
  assert.deepEqual(rows.map((r) => r.name), ["claude", "codex", "vibe", "agy", "grok"]);  // ours only, no "pi"
  // herdr finds it (a login shell function, say): installed, so no install action
  assert.equal(by.claude.installed, true);
  assert.equal(by.claude.install, null);
  assert.equal(by.vibe.selectable, true);
  // missing everywhere: greyed, with the official installer in a terminal tab
  assert.equal(by.agy.installed, false);
  assert.equal(by.agy.selectable, false);
  assert.deepEqual(by.agy.install, { name: "Install Antigravity", argv: [CLI, "harness", "install", "agy"] });
  // installed but refused by the CLI: greyed with its reason, nothing to install
  assert.equal(by.grok.selectable, false);
  assert.equal(by.grok.install, null);
  assert.equal(by.grok.note, "connects through your account");
  // knownAgents() failed: the CLI's own answer decides
  const alone = harnessRows([offer("codex", false, false, "not installed (codex)")], undefined, CLI);
  assert.deepEqual(alone[0].install, { name: "Install Codex", argv: [CLI, "harness", "install", "codex"] });
});

test("exit 3 opens the connect dialog", () => {
  const form = { name: "scout", harness: "codex", role: "Research", skill: "" };
  const next = settle(form, { code: 3, stdout: "", stderr: "This Herdr is not connected: run moshi-cockpit connect" });
  assert.equal(next.action, "connect");
  assert.equal(next.problem, null);
  assert.equal(settle(form, { code: 0, stdout: "• Agent scout (codex) created\n", stderr: "" }).action, "done");
});

test("taken name keeps the typed values", () => {
  const form = { name: "scout", harness: "codex", role: "Research", skill: "skill-a" };
  const next = settle(form, { code: 1, stdout: "", stderr: "Error: That name is taken in moshi.\n" });
  assert.equal(next.action, "stay");
  assert.equal(next.problem, "That name is taken in moshi.");
  assert.deepEqual(next.form, form);
  // control characters from a process never reach the dialog
  assert.equal(settle(form, { code: 1, stdout: "", stderr: "Error: bad\u001b[31m\n" }).problem, "bad [31m");
  assert.match(settle(form, { code: 1, stdout: "", stderr: "" }).problem, /failed/);
});

test("catalog rows show where an agent runs", () => {
  const rows = catalogRows([
    { name: "scout", harness: "codex", role: "Research", skill: "", active_installation: "Laptop",
      harness_installed: true, loadable: true, reason: "" },
    { name: "writer", harness: "vibe", role: "", skill: "", active_installation: null,
      harness_installed: false, loadable: true, reason: "" },
    { name: "two", harness: "agy", role: "", skill: "", active_installation: null,
      harness_installed: true, loadable: false, reason: "one agy agent per Mac, taken by 'a1'" },
  ]);
  assert.deepEqual(rows.map((r) => [r.name, r.harness, r.where]), [["scout", "codex", "Running on Laptop"],
                                                                  ["writer", "vibe", ""], ["two", "agy", ""]]);
  assert.deepEqual(rows.map((r) => r.loadable), [true, true, false]);
  assert.equal(rows[1].note, "vibe is not installed here");
  assert.equal(rows[2].note, "one agy agent per Mac, taken by 'a1'");
  assert.equal(catalogRows([{ name: "x\u0007", harness: "codex", active_installation: "A\u001bB", loadable: true }])[0].where,
               "Running on A B");
  assert.deepEqual(catalogRows(undefined), []);
});

test("start asks before it takes over an agent that runs elsewhere", () => {
  const where = (body, code = 0) => ({ code, stdout: JSON.stringify(body), stderr: "" });
  assert.deepEqual(takeoverFrom(where({ active: "Laptop", here: false })),
                   { kind: "ask", label: "Laptop", question: "Running on Laptop. Take over here?" });
  assert.equal(takeoverFrom(where({ active: "Desk", here: true })).kind, "start");
  assert.equal(takeoverFrom(where({ active: null, here: false })).kind, "start");
  assert.equal(takeoverFrom({ code: 3, stdout: "", stderr: "" }).kind, "connect");
  // no moshi, moshi not reachable, or an answer that is no JSON: run decides, as it would from a terminal
  assert.equal(takeoverFrom({ code: 1, stdout: "", stderr: "Error: moshi is not set up" }).kind, "start");
  assert.equal(takeoverFrom({ code: 0, stdout: "nope", stderr: "" }).kind, "start");
});

test("sidebar line for disconnected and unreachable", () => {
  assert.deepEqual(sidebarLine({ disconnected: true, unreachable: false }),
                   { text: "Disconnected from moshi — Connect again", connect: true });
  assert.deepEqual(sidebarLine({ disconnected: false, unreachable: true }), { text: "moshi not reachable", connect: false });
  assert.deepEqual(sidebarLine({ needs_connect: true, disconnected: false, unreachable: false }),
                   { text: "Not connected to moshi", connect: true });
  assert.equal(sidebarLine({ disconnected: false, unreachable: false, needs_connect: false }), null);
  assert.equal(sidebarLine(null), null);
});
