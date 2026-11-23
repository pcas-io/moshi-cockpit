// The "+ Agent" dialog, the takeover question and the sidebar's connection line. Run: node --test tests/js/
import { test } from "node:test";
import assert from "node:assert/strict";
import { HERDR_APP, harnessRows } from "../../share/herdr-mods/mesh-agents/harnesses.js";
import { catalogRows, settle } from "../../share/herdr-mods/mesh-agents/catalog.js";
import { takeoverFrom } from "../../share/herdr-mods/mesh-agents/rules.js";
import { ownAgents, remoteCommand, sidebarLine } from "../../share/herdr-mods/moshi/list.js";

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
  // herdr finds it (a login shell function, say), the CLI does not: `run` could not start it, so not selectable
  assert.equal(by.claude.installed, false);
  assert.equal(by.claude.selectable, false);
  assert.equal(by.claude.herdrOnly, true);
  assert.equal(by.claude.note, "found by Herdr, not on moshi-cockpit's PATH");
  assert.deepEqual(by.claude.install, { name: "Install Claude Code", argv: [CLI, "harness", "install", "claude"] });
  // the CLI finds it: selectable whatever herdr says
  assert.equal(by.vibe.selectable, true);
  assert.equal(by.vibe.note, "");
  // missing everywhere: greyed, with the official installer in a terminal tab
  assert.equal(by.agy.installed, false);
  assert.equal(by.agy.selectable, false);
  assert.equal(by.agy.herdrOnly, false);
  assert.equal(by.agy.note, "not installed");
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
  // From moshi on a remote machine: the note names that machine, the reason is the remote's own ("per machine")
  const remote = catalogRows([
    { name: "writer", harness: "vibe", active_installation: null, harness_installed: false, loadable: true, reason: "" },
    { name: "two", harness: "agy", active_installation: null, harness_installed: true, loadable: false,
      reason: "one agy agent per machine, taken by 'a1'" }], "devbox");
  assert.deepEqual(remote.map((r) => r.note), ["vibe is not installed on devbox", "one agy agent per machine, taken by 'a1'"]);
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

const LISTED = [{ name: null, layout: "ready" }, { name: "devbox", layout: "ready" }, { name: "build-1", layout: "loading" }];

test("remote agents join the sidebar once, with their machine", () => {
  const argv = ["/bin/sh", "-lc", "x", "sh", "r2"];
  const rows = ownAgents([{ name: "ok-1", harness: "codex", installed: true }],
                         [{ name: "r2", presence: "live", working_on: "tests", last_seen_at: "2026-11-18T10:00:00Z" }],
                         [{ name: "OK-1", harness: "codex", role: "", machine: "devbox", run_argv: ["x"] },
                          { name: "r2", harness: "claude", role: "Review", machine: "build-1", run_argv: argv },
                          { name: "R2", harness: "codex", role: "", machine: "devbox", run_argv: ["y"] }],
                         LISTED);
  assert.deepEqual(rows.map((r) => [r.name, r.machine]), [["r2", "build-1"], ["ok-1", null]]);
  const [r2, ok] = rows;
  assert.deepEqual(r2, { name: "r2", harness: "claude", role: "Review", installed: true, machine: "build-1",
                         runArgv: argv, presence: "live", working_on: "tests", last_seen_at: "2026-11-18T10:00:00Z" });
  assert.equal(ok.machine, null);
  assert.equal(ok.presence, null);
  // control characters in a machine name never reach the row; no remote list at all is today's list
  assert.equal(ownAgents([], [], [{ name: "a", harness: "codex", machine: "dev\u001bbox", run_argv: ["x"] }],
                         [{ name: "dev\u001bbox", layout: "ready" }])[0].machine, "dev box");
  assert.deepEqual(ownAgents([{ name: "ok-1", harness: "codex" }], []).map((r) => r.machine), [null]);
});

test("remote agents show only for machines Herdr still lists", () => {
  const remote = [{ name: "gone-1", harness: "codex", machine: "old-box", run_argv: ["x"] },
                  { name: "r2", harness: "codex", machine: "old-box", run_argv: ["x"] },
                  { name: "R2", harness: "claude", machine: "devbox", run_argv: ["y"] }];
  // old-box was removed from Herdr: its rows go, and its r2 does not hide devbox's
  assert.deepEqual(ownAgents([], [], remote, LISTED).map((r) => [r.name, r.machine]), [["R2", "devbox"]]);
  assert.deepEqual(ownAgents([], [], remote, [{ name: null, layout: "ready" }]), []);  // This Mac only
  assert.deepEqual(ownAgents([], [], remote), []);
  assert.deepEqual(ownAgents([], [], remote, undefined), []);
});

test("remote start lands in a space on that machine", () => {
  const argv = ["/bin/sh", "-lc", "x", "sh", "r2"];
  const a = { name: "r2", machine: "devbox", runArgv: argv };
  assert.deepEqual(remoteCommand([{ id: "s0", machine: null }, { id: "s1", machine: "devbox" }, { id: "s2", machine: "devbox" }], a),
                   { command: "tab.new", args: { name: "r2", argv, space: "s1" } });
  assert.deepEqual(remoteCommand([{ id: "s0", machine: null }, { id: "s3", machine: "build-1" }], a),
                   { command: "space.new", args: { machine: "devbox", name: "r2", argv } });
  assert.deepEqual(remoteCommand(undefined, a), { command: "space.new", args: { machine: "devbox", name: "r2", argv } });
  assert.equal(remoteCommand([], { ...a, machine: "-x" }), null);
  assert.equal(remoteCommand([], { ...a, machine: null }), null);
  assert.equal(remoteCommand([], { ...a, runArgv: [] }), null);
  assert.equal(remoteCommand([], { ...a, runArgv: undefined }), null);
});
