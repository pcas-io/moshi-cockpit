// moshi-cockpit on another machine from "+ Agent": the Machines tab's link, New's moshi form and From moshi there.
// Run: node --test tests/js/
import { test } from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { AGENT_EXEC, COCKPIT_EXEC, cockpitArgs, cockpitLink, moshiChips, moshiReady, remoteAddArgs, remoteCatalogArgs,
         remoteLoadArgs, remoteSettle } from "../../share/herdr-mods/mesh-agents/remote.js";

const INSTALL = ["/bin/sh", "-lc", "install", "sh", "https://example.invalid/install.sh", "", ""];
const CONNECT = ["/bin/sh", "-lc", 'exec "$HOME/.local/bin/moshi-cockpit" connect --label "$1"', "sh", "devbox"];
const answer = (state, extra = {}) => ({ machine: "devbox", os: "Linux", state, state_text: `text of ${state}`, update: false,
                                         agents: [], harnesses: [], ocellio: false, install_argv: null, connect_argv: null, ...extra });
const source = (file) => readFileSync(new URL(`../../share/herdr-mods/mesh-agents/${file}`, import.meta.url), "utf8");

test("cockpit args", () => {
  assert.deepEqual(cockpitArgs("devbox", false), ["machine", "cockpit", "--on", "devbox", "--json"]);
  assert.deepEqual(cockpitArgs("build-1", true), ["machine", "cockpit", "--on", "build-1", "--refresh", "--json"]);
});

test("exec waits longer than the CLI guards", () => {
  // above the CLI's own guards (45 s for the probe, 120 s for an agent call), so a slow machine ends in its answer
  assert.ok(COCKPIT_EXEC.timeout > 45000);
  assert.ok(AGENT_EXEC.timeout > 120000 + 45000);  // add or load, then the re-probe
  let cockpitCalls = 0;
  let agentCalls = 0;
  for (const file of ["machines-view.js", "main.js"]) {
    for (const line of source(file).split("\n")) {
      for (const call of line.match(/cockpitArgs\(.*/g) ?? []) {
        cockpitCalls += 1;
        assert.match(call, /^cockpitArgs\([^)]*\), true, COCKPIT_EXEC\)/, `${file}: ${call}`);
      }
      for (const call of line.match(/remote(Add|Load|Catalog)Args\(.*/g) ?? []) {
        agentCalls += 1;
        assert.match(call, /^remote(Add|Load|Catalog)Args\([^)]*\), AGENT_EXEC\)/, `${file}: ${call}`);
      }
    }
  }
  assert.ok(cockpitCalls >= 2, "machines-view.js and main.js ask for the state");
  assert.ok(agentCalls >= 3, "add, load and catalog");
});

test("cockpit link per state", () => {
  assert.deepEqual(cockpitLink(answer("missing", { install_argv: INSTALL })),
                   { text: "moshi: Install", tooltip: "text of missing", action: { name: "Install moshi-cockpit", argv: INSTALL } });
  // a version mismatch offers Update, even while connected
  assert.deepEqual(cockpitLink(answer("connected", { update: true, install_argv: INSTALL, connect_argv: CONNECT })),
                   { text: "moshi: Update", tooltip: "text of connected", action: { name: "Update moshi-cockpit", argv: INSTALL } });
  assert.deepEqual(cockpitLink(answer("unconnected", { connect_argv: CONNECT })),
                   { text: "moshi: Connect", tooltip: "text of unconnected", action: { name: "Connect to moshi", argv: CONNECT } });
  const two = [{ name: "a", harness: "codex" }, { name: "b", harness: "claude" }];
  const connected = cockpitLink(answer("connected", { agents: two, connect_argv: CONNECT }));
  assert.equal(connected.text, "moshi ✓ 2");
  assert.equal(connected.action, null);
  assert.equal(cockpitLink(answer("unsupported", { os: "Darwin" })), null);
  assert.equal(cockpitLink(null), null);
  assert.equal(cockpitLink(undefined), null);
  assert.deepEqual(cockpitLink(answer("error", { state_text: "devbox did not answer:\u001b[31m timeout" })),
                   { text: "moshi: ?", tooltip: "devbox did not answer: [31m timeout", action: null });
  // an unreadable version is the error state: "moshi: ?" with the reason, never Update
  const unreadable = "moshi-cockpit there reports no version this Mac can read, so it is not offered an update; run x";
  assert.deepEqual(cockpitLink(answer("error", { state_text: unreadable, update: true, install_argv: INSTALL })),
                   { text: "moshi: ?", tooltip: unreadable, action: null });
  // a newer moshi-cockpit there (the CLI says update: false) shows ✓ as usual, never a downgrade
  assert.deepEqual(cockpitLink(answer("connected", { version: "99.0.0", agents: two, connect_argv: CONNECT })),
                   { text: "moshi ✓ 2", tooltip: "text of connected", action: null });
});

test("moshi ready only when connected and current", () => {
  assert.equal(moshiReady(answer("connected")), true);
  assert.equal(moshiReady(answer("connected", { update: true })), false);
  assert.equal(moshiReady(answer("unconnected")), false);
  assert.equal(moshiReady(null), false);
});

test("moshi chips", () => {
  const chips = moshiChips([{ name: "codex", installed: true, available: true },
                            { name: "agy", installed: true, available: false, reason: "one agent per machine, taken by 'a1'" },
                            { name: "vibe", installed: false }], "devbox");
  assert.deepEqual(chips, [
    { name: "codex", label: "Codex", selectable: true, note: "" },
    { name: "agy", label: "Antigravity", selectable: false, note: "Antigravity: one agent per machine, taken by 'a1'" },
    { name: "vibe", label: "Vibe", selectable: false, note: "Vibe is not installed on devbox" },
  ]);
  assert.deepEqual(moshiChips(undefined, "devbox"), []);
  assert.equal(moshiChips([{ name: "vibe", installed: false }], "dev\u001bbox")[0].note, "Vibe is not installed on dev box");
  // the form shows the notes as they are, so each one names its harness
  assert.match(source("main.js"), /notes\.map\(\(c\) => c\.note\)\.join\(" \\u00b7 "\)/);
});

test("remote add args keep the typed text as one argument", () => {
  const form = { name: "research", harness: "codex", role: "reviews; rm -rf ~", skill: "code-review" };
  assert.deepEqual(remoteAddArgs("devbox", form), ["machine", "agent", "--on", "devbox", "add", "research", "--harness", "codex",
                                                   "--role=reviews; rm -rf ~", "--skill=code-review", "--json"]);
  assert.equal(remoteAddArgs("devbox", { ...form, role: "-x" })[8], "--role=-x");
  assert.deepEqual(remoteLoadArgs("build-1", "research"), ["machine", "agent", "--on", "build-1", "load", "research", "--json"]);
  assert.deepEqual(remoteCatalogArgs("devbox"), ["machine", "agent", "--on", "devbox", "catalog", "--json"]);
});

test("settle", () => {
  const runArgv = ["/bin/sh", "-lc", 'exec "$HOME/.local/bin/moshi-cockpit" run "$1"', "sh", "research"];
  assert.deepEqual(remoteSettle({ code: 0, stdout: JSON.stringify({ ok: true, code: 0, notes: [], message: "", run_argv: runArgv }), stderr: "" }),
                   { action: "start", runArgv });
  assert.deepEqual(remoteSettle({ code: 3, stdout: JSON.stringify({ ok: false, code: 3, message: "Approval required" }), stderr: "" }),
                   { action: "connect", problem: "The approval on this machine is missing or expired: Connect to moshi there" });
  assert.deepEqual(remoteSettle({ code: 1, stdout: '{"message":"Error x"}', stderr: "" }), { action: "stay", problem: "Error x" });
  assert.deepEqual(remoteSettle({ code: 1, stdout: "MOTD noise", stderr: "devbox did not answer: exit code 255" }),
                   { action: "stay", problem: "devbox did not answer: exit code 255" });
  assert.deepEqual(remoteSettle({ code: 1, stdout: "", stderr: "" }), { action: "stay", problem: "moshi-cockpit failed (exit 1)" });
  // a success without a command to start is not a start
  assert.equal(remoteSettle({ code: 0, stdout: '{"ok":true,"run_argv":null}', stderr: "" }).action, "stay");
});
