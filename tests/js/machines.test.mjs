// The Machines tab of "+ Agent": machine rows, harness actions and the tabs they open. Run: node --test tests/js/
import { test } from "node:test";
import assert from "node:assert/strict";
import { COLUMNS, GLYPH, HERDR_KEYS, INVENTORY_EXEC, LEGEND, STATE_TEXT, addHostCommand, cellAction, connectingNote, harnessAction,
         hostRows, inventoryArgs, layoutReady, machineRows, matrixRow, passable, remoteChips, remoteStart, tabCommand,
         whereChoices }
  from "../../share/herdr-mods/mesh-agents/machines.js";
import { readFileSync } from "node:fs";

const CLI = "/Users/x/.local/bin/moshi-cockpit";
const HELD = (cmd) => ["/bin/sh", "-lc", `${cmd}; read -r _`];
const row = (state, extra = {}) => ({
  name: "codex", label: "Codex", state, state_text: STATE_TEXT[state], install_argv: null, install_command: null,
  source_url: null, login_argv: null, start_argv: ["/bin/sh", "-lc", "exec codex"], ...extra,
});

test("inventory args", () => {
  assert.deepEqual(STATE_TEXT, { ready: "Ready", installed: "Installed, not signed in", missing: "Missing" });
  assert.deepEqual(inventoryArgs(null, false), ["machine", "inventory", "--json"]);
  assert.deepEqual(inventoryArgs(null, true), ["machine", "inventory", "--refresh", "--json"]);
  assert.deepEqual(inventoryArgs("devbox", false), ["machine", "inventory", "--on", "devbox", "--json"]);
  assert.deepEqual(inventoryArgs("build-1", true), ["machine", "inventory", "--on", "build-1", "--refresh", "--json"]);
});

test("inventory waits longer than the CLI's own 45 s guard", () => {
  // exec's timeout above PROBE_TIMEOUT (cockpit/machines.py), so a slow machine shows the CLI's "did not answer"
  assert.deepEqual(INVENTORY_EXEC, { timeout: 60000 });
  // every inventory call of the views (util.js imports the host, so the call is checked in the source)
  for (const file of ["machines-view.js", "main.js"]) {
    const source = readFileSync(new URL(`../../share/herdr-mods/mesh-agents/${file}`, import.meta.url), "utf8");
    const calls = source.match(/cockpit\(exec, home, inventoryArgs\([^)]*\)[^)]*\)/g) ?? [];
    assert.ok(calls.length > 0, file);
    for (const call of calls) assert.match(call, /, true, INVENTORY_EXEC\)$/, `${file}: ${call}`);
  }
});

test("machine rows put this Mac first and explain disconnected machines", () => {
  const rows = machineRows({ machines: [{ name: "devbox", state: "connected", target: "me@devbox" },
                                        { name: "build-1", state: "needs_login", target: "me@build-1" }],
                             ssh_hosts: [] });
  assert.equal(rows.length, 3);
  assert.deepEqual(rows[0], { name: null, label: "This Mac", connected: true, note: "" });
  assert.deepEqual(rows[1], { name: "devbox", label: "devbox", connected: true, note: "" });
  assert.deepEqual(rows[2], { name: "build-1", label: "build-1", connected: false,
                              note: "needs login: inventory once it is connected" });
  // no list (Herdr's machine list was not available): this Mac alone
  assert.deepEqual(machineRows(null).map((r) => r.label), ["This Mac"]);
  assert.deepEqual(machineRows({ machines: [], problem: "x" }).map((r) => r.label), ["This Mac"]);
  // no state at all reads as not connected
  for (const state of ["", undefined, null]) {
    assert.equal(machineRows({ machines: [{ name: "devbox", state }] })[1].note,
                 "not connected: inventory once it is connected");
  }
  // a name hc could read as an option or shell text is listed, never asked: no inventory, no actions
  for (const name of ["-oProxyCommand=x", "a b", "a;b"]) {
    const [, odd] = machineRows({ machines: [{ name, state: "connected" }] });
    assert.equal(odd.connected, false, name);
    assert.equal(odd.note, "name not supported", name);
  }
  assert.equal(machineRows({ machines: [{ name: "x\u001b[2Jy", state: "connected" }] })[1].label, "x [2Jy");
});

test("remote install asks inline, local install leaves the question to the CLI", () => {
  const remote = harnessAction(row("missing", { install_argv: HELD("curl -fsSL x | sh"), install_command: "curl -fsSL x | sh" }), "devbox");
  assert.equal(remote.kind, "install");
  assert.equal(remote.label, "Install");
  assert.deepEqual(remote.argv, HELD("curl -fsSL x | sh"));
  assert.equal(remote.confirm, "Runs curl -fsSL x | sh on devbox as your SSH user, in a new tab.");
  const local = harnessAction(row("missing", { install_argv: [CLI, "harness", "install", "codex"],
                                               install_command: "curl -fsSL x | sh" }), null);
  assert.deepEqual(local, { kind: "install", label: "Install", argv: [CLI, "harness", "install", "codex"],
                            confirm: null, note: "" });
});

test("remote install without curl offers no action", () => {
  const action = harnessAction(row("missing", { install_command: "curl -fsSL x | sh" }), "build-1");
  assert.equal(action.kind, null);
  assert.equal(action.argv, null);
  assert.equal(action.confirm, null);
  assert.equal(action.note, "needs curl on build-1");
});

test("ready rows have no action", () => {
  assert.deepEqual(harnessAction(row("ready"), "devbox"), { kind: null, label: "", argv: null, confirm: null, note: "" });
  assert.equal(harnessAction(row("ready"), null).kind, null);
  // signed out: the vendor's login command, in a tab, without asking first
  const signIn = harnessAction(row("installed", { login_argv: HELD("codex login --device-auth") }), "devbox");
  assert.deepEqual(signIn, { kind: "sign-in", label: "Sign in", argv: HELD("codex login --device-auth"), confirm: null, note: "" });
  // no login command: the first start signs in
  assert.deepEqual(harnessAction(row("installed"), null).argv, ["/bin/sh", "-lc", "exec codex"]);
});

test("tab command reuses a space on that machine or opens one", () => {
  const argv = ["/bin/sh", "-lc", "x"];
  const spaces = [{ id: "build-1/s0", machine: "build-1" }, { id: "s1", machine: null },
                  { id: "build-1/s2", machine: "build-1" }, { id: "s3" }];
  // this Mac: its first local space, never a remote one listed before it
  assert.deepEqual(tabCommand(spaces, null, "Install Codex", argv),
                   { command: "tab.new", args: { name: "Install Codex", argv, space: "s1" } });
  assert.deepEqual(tabCommand([{ id: "s3" }], null, "x", argv), { command: "tab.new", args: { name: "x", argv, space: "s3" } });
  // no local space (only remote ones, or none): a new local space, without a machine
  assert.deepEqual(tabCommand([{ id: "build-1/s0", machine: "build-1" }], null, "Install Codex", argv),
                   { command: "space.new", args: { name: "Install Codex", argv } });
  assert.deepEqual(tabCommand(null, null, "x", argv), { command: "space.new", args: { name: "x", argv } });
  assert.deepEqual(tabCommand(spaces, "build-1", "Install Codex", argv),
                   { command: "tab.new", args: { name: "Install Codex", argv, space: "build-1/s0" } });
  assert.deepEqual(tabCommand(spaces, "devbox", "Install Codex", argv),
                   { command: "space.new", args: { machine: "devbox", name: "Install Codex", argv } });
  assert.deepEqual(tabCommand(null, "devbox", "x", argv).command, "space.new");
});

test("add host command runs the CLI in a visible tab", () => {
  const argv = [CLI, "machine", "add", "devbox"];
  const spaces = [{ id: "devbox/s1", machine: "devbox" }, { id: "s2", machine: null }];
  assert.deepEqual(addHostCommand(spaces, CLI, "devbox"),
                   { command: "tab.new", args: { name: "Add devbox", argv, space: "s2" } });  // never a remote space
  assert.deepEqual(addHostCommand([{ id: "devbox/s1", machine: "devbox" }], CLI, "devbox"),
                   { command: "space.new", args: { name: "Add devbox", argv } });
  // only what hc reads as a name gets an Add button (cockpit/machines.py NAME_RE): never an option or shell text
  for (const ok of ["devbox", "build-1", "me@build-1", "build-1.lan:22"]) assert.equal(passable(ok), true, ok);
  for (const bad of ["", "-oProxyCommand=x", "a b", "a;b", "$(x)", "_x", "x".repeat(255), null]) assert.equal(passable(bad), false, String(bad));
  // the raw alias is checked, then shortened for display only: a long one is never cut into a valid one
  const rows = hostRows(["devbox", "-oX", "a".repeat(300), "", null]);
  assert.deepEqual(rows[0], { alias: "devbox", label: "devbox", ok: true, note: "" });
  assert.deepEqual(rows[1], { alias: "-oX", label: "-oX", ok: false, note: "can't be added from here" });
  assert.equal(rows[2].ok, false);
  assert.equal(rows[2].label.length, 254);
  assert.equal(rows.length, 3);  // nothing to show for an empty entry
  assert.deepEqual(hostRows(undefined), []);
});

test("where choices list ready machines after this Mac", () => {
  const ms = [{ name: null, layout: "ready" }, { name: "devbox", layout: "ready" }, { name: "build-1", layout: "loading" },
              { name: "build-2", layout: "unavailable" }, { name: "build-3", layout: "ready" }];
  assert.deepEqual(whereChoices(ms), [{ name: null, label: "This Mac" }, { name: "devbox", label: "devbox" },
                                      { name: "build-3", label: "build-3" }]);
  // no machines() answer: this Mac alone
  assert.deepEqual(whereChoices(undefined), [{ name: null, label: "This Mac" }]);
  assert.deepEqual(whereChoices([]), [{ name: null, label: "This Mac" }]);
  // a name hc could read as an option or shell text never reaches knownAgents({on}) or space.new {machine}
  const odd = ["-oProxyCommand=x", "a b", "a;b", "$(x)", "", 7].map((name) => ({ name, layout: "ready" }));
  assert.deepEqual(whereChoices(odd), [{ name: null, label: "This Mac" }]);
});

test("remote chips allow installed and ready, explain missing", () => {
  const rows = [row("ready", { name: "claude", label: "Claude Code" }), row("installed"),
                row("missing", { name: "opencode", label: "OpenCode" })];
  assert.deepEqual(remoteChips(rows, "devbox"), [
    { name: "claude", label: "Claude Code", selectable: true, note: "" },
    { name: "codex", label: "Codex", selectable: true, note: "" },
    { name: "opencode", label: "OpenCode", selectable: false, note: "missing on devbox: install it under Machines" },
  ]);
  // no inventory yet (loading or failed): no chips, never a guessed "Ready"
  assert.deepEqual(remoteChips(null, "devbox"), []);
  // only the six harnesses, and nothing selectable from a state the inventory does not know
  assert.deepEqual(remoteChips([row("ready", { name: "pi" })], "devbox"), []);
  assert.equal(remoteChips([row("broken")], "devbox")[0].selectable, false);
  assert.equal(remoteChips([row("ready", { label: "Co\u001b[2Jdex" })], "devbox")[0].label, "Co [2Jdex");
});

test("a harness Herdr finds there can start even when the probe says missing", () => {
  const rows = [row("missing"), row("missing", { name: "opencode", label: "OpenCode" })];
  const known = [{ key: "codex", name: "Codex", run: ["codex"], installed: true },
                 { key: "opencode", name: "OpenCode", run: ["opencode"], installed: false }];
  assert.deepEqual(remoteChips(rows, "devbox", known), [
    { name: "codex", label: "Codex", selectable: true, note: "" },
    { name: "opencode", label: "OpenCode", selectable: false, note: "missing on devbox: install it under Machines" },
  ]);
  // Herdr's run is what starts it
  assert.deepEqual(remoteStart("devbox", rows[0], known), { machine: "devbox", name: "Codex", argv: ["codex"] });
  // installed without a command to run, or knownAgents() failed: still missing
  assert.equal(remoteChips(rows, "devbox", [{ key: "codex", run: [], installed: true }])[0].selectable, false);
  assert.equal(remoteChips(rows, "devbox", null)[0].selectable, false);
});

test("remote start uses Herdr's own command when it knows the harness", () => {
  assert.deepEqual(HERDR_KEYS, { claude: "claude", codex: "codex", vibe: "vibe", agy: "antigravity", grok: "grok",
                                 opencode: "opencode" });
  const known = [{ key: "codex", name: "Codex", run: ["codex"], installed: true },
                 { key: "antigravity", name: "Antigravity", run: ["agy", "--x"], installed: true }];
  assert.deepEqual(remoteStart("devbox", row("ready"), known), { machine: "devbox", name: "Codex", argv: ["codex"] });
  // our key → Herdr's key (agy is "antigravity" there)
  assert.deepEqual(remoteStart("build-1", row("installed", { name: "agy", label: "Antigravity" }), known).argv, ["agy", "--x"]);
});

test("remote start falls back to the inventory's start argv", () => {
  const start = ["/bin/sh", "-lc", "exec codex"];
  // Herdr knows it, but its shell does not find it there
  assert.deepEqual(remoteStart("devbox", row("ready"), [{ key: "codex", run: ["codex"], installed: false }]),
                   { machine: "devbox", name: "Codex", argv: start });
  // Herdr does not know it, or knownAgents() failed, or gave no command
  assert.deepEqual(remoteStart("devbox", row("ready"), [{ key: "claude", run: ["claude"], installed: true }]).argv, start);
  assert.deepEqual(remoteStart("devbox", row("ready"), null).argv, start);
  assert.deepEqual(remoteStart("devbox", row("ready"), [{ key: "codex", run: [], installed: true }]).argv, start);
});

test("install and sign in wait until Herdr has the machine's layout", () => {
  const ms = [{ name: null, layout: "ready" }, { name: "devbox", layout: "ready" }, { name: "build-1", layout: "loading" },
              { name: "build-2", layout: "unavailable" }];
  assert.equal(layoutReady(ms, null), true);  // this Mac always
  assert.equal(layoutReady(undefined, null), true);
  assert.equal(layoutReady(ms, "devbox"), true);
  for (const name of ["build-1", "build-2", "build-9"]) assert.equal(layoutReady(ms, name), false, name);
  assert.equal(layoutReady(undefined, "devbox"), false);
  const missing = row("missing", { install_argv: HELD("curl -fsSL x | sh"), install_command: "curl -fsSL x | sh" });
  const signedOut = row("installed", { login_argv: HELD("codex login --device-auth") });
  const none = { kind: null, label: "", argv: null, confirm: null, note: "" };
  for (const r of [missing, signedOut, row("missing", { install_command: "x" })]) {
    assert.deepEqual(harnessAction(r, "build-1", layoutReady(ms, "build-1")), none);
  }
  assert.equal(harnessAction(missing, "devbox", layoutReady(ms, "devbox")).kind, "install");
  assert.equal(harnessAction(signedOut, "devbox", true).kind, "sign-in");
  assert.equal(harnessAction(signedOut, null, layoutReady(ms, null)).kind, "sign-in");
  assert.equal(connectingNote("build-1"), "Herdr is still connecting to build-1");
});

// The matrix: one row per machine, one column per harness, fixed widths so every row lines up.
const MAC = { name: null, label: "This Mac", connected: true, note: "" };
const DEVBOX = { name: "devbox", label: "devbox", connected: true, note: "" };
const ago = (iso) => `ago(${iso})`;
const six = (states) => COLUMNS.map((c, i) => row(states[i], { name: c.name, label: c.name.toUpperCase(),
  ...(states[i] === "missing" ? { install_argv: HELD("curl x | sh"), install_command: "curl x | sh" } : {}) }));
const inv = (harnesses, extra = {}) => ({ loading: false, error: null, data: { checked_at: "T", harnesses }, ...extra });

test("matrix columns are the six harnesses with short labels", () => {
  assert.deepEqual(COLUMNS.map((c) => c.name), ["claude", "codex", "vibe", "agy", "grok", "opencode"]);
  assert.deepEqual(COLUMNS.map((c) => c.short), ["Claude", "Codex", "Vibe", "Agy", "Grok", "OpenCode"]);
  assert.deepEqual(Object.keys(HERDR_KEYS), COLUMNS.map((c) => c.name));
  assert.deepEqual(GLYPH, { ready: "\u25cf", "sign-in": "\u25d0", install: "\u25cb" });
  assert.deepEqual(LEGEND, [{ show: "ready", text: "Ready" }, { show: "sign-in", text: "Sign in" },
                            { show: "install", text: "Install" }]);
});

test("a cell shows ready, sign in or install, and keeps harnessAction's action", () => {
  const ready = cellAction(row("ready"), "devbox");
  assert.equal(ready.show, "ready");
  assert.equal(ready.action.kind, null);
  assert.equal(ready.hint, "Codex: Ready");
  const signedOut = row("installed", { login_argv: HELD("codex login --device-auth") });
  const signIn = cellAction(signedOut, "devbox");
  assert.equal(signIn.show, "sign-in");
  assert.deepEqual(signIn.action, harnessAction(signedOut, "devbox"));
  assert.equal(signIn.hint, "Codex: Installed, not signed in. Click to sign in.");
  const missing = row("missing", { install_argv: HELD("curl x | sh"), install_command: "curl x | sh" });
  const install = cellAction(missing, "devbox");
  assert.equal(install.show, "install");
  assert.deepEqual(install.action, harnessAction(missing, "devbox"));
  assert.equal(install.action.confirm, "Runs curl x | sh on devbox as your SSH user, in a new tab.");
  assert.equal(install.hint, "Codex: Missing. Click to install.");
  // this Mac: the CLI asks in its tab
  assert.equal(cellAction(row("missing", { install_argv: [CLI, "harness", "install", "codex"] }), null).action.confirm, null);
  // no curl there: an empty cell, the reason as its hint
  const noCurl = cellAction(row("missing", { install_command: "x" }), "build-1");
  assert.equal(noCurl.show, null);
  assert.equal(noCurl.hint, "Codex: Missing, needs curl on build-1");
  // a state the inventory does not know, or no row at all: empty, never a guessed "Ready"
  assert.equal(cellAction(row("broken"), "devbox").show, null);
  assert.equal(cellAction(undefined, "devbox").show, null);
  assert.equal(cellAction(row("ready", { label: "Co\u001b[2Jdex" }), null).hint, "Co [2Jdex: Ready");
});

test("matrix row: cells in column order once the inventory is in", () => {
  const r = matrixRow(DEVBOX, inv(six(["ready", "installed", "missing", "ready", "missing", "installed"]).reverse()), true, ago);
  assert.deepEqual(r.cells.map((c) => [c.name, c.short, c.show]), [
    ["claude", "Claude", "ready"], ["codex", "Codex", "sign-in"], ["vibe", "Vibe", "install"],
    ["agy", "Agy", "ready"], ["grok", "Grok", "install"], ["opencode", "OpenCode", "sign-in"]]);
  assert.equal(r.status, "checked ago(T)");
  assert.equal(r.note, "");
  assert.equal(r.error, "");
  assert.equal(r.refresh, true);
  assert.equal(r.loading, false);
  // a harness the inventory does not list: its column stays, empty
  const partial = matrixRow(MAC, inv([row("ready", { name: "grok", label: "Grok" })]), true, ago);
  assert.equal(partial.cells.length, 6);
  assert.deepEqual(partial.cells.map((c) => c.show), [null, null, null, null, "ready", null]);
  // refreshing keeps the last answer on screen
  const again = matrixRow(DEVBOX, inv(six(Array(6).fill("ready")), { loading: true }), true, ago);
  assert.equal(again.status, "Checking\u2026");
  assert.equal(again.loading, true);
  assert.equal(again.cells.length, 6);
});

test("matrix row: notes replace the cells", () => {
  // not connected, or a name hc could read as an option: the machine's note, no cells, no Refresh
  const asleep = matrixRow({ name: "build-1", label: "build-1", connected: false, note: "asleep: inventory once it is connected" },
                           undefined, false, ago);
  assert.deepEqual(asleep, { label: "build-1", status: "", note: "asleep: inventory once it is connected", error: "",
                             refresh: false, loading: false, cells: [] });
  const odd = matrixRow({ name: "a b", label: "a b", connected: false, note: "name not supported" },
                        inv(six(Array(6).fill("ready"))), true, ago);
  assert.equal(odd.note, "name not supported");
  assert.deepEqual(odd.cells, []);
  // first check running
  assert.deepEqual(matrixRow(DEVBOX, { loading: true, error: null }, true, ago),
                   { label: "devbox", status: "Checking\u2026", note: "", error: "", refresh: true, loading: true, cells: [] });
  assert.equal(matrixRow(MAC, undefined, true, ago).status, "");
  // the machine did not answer: one error line, nothing shown as Ready
  const failed = matrixRow(DEVBOX, { loading: false, data: null, error: "devbox did not answer: timeout" }, true, ago);
  assert.equal(failed.error, "devbox did not answer: timeout");
  assert.deepEqual(failed.cells, []);
  assert.equal(failed.refresh, true);
  // Herdr has not read the machine's layout yet: no cells, since no tab could open there
  const connecting = matrixRow(DEVBOX, inv(six(Array(6).fill("missing"))), false, ago);
  assert.equal(connecting.note, "Herdr is still connecting to devbox");
  assert.deepEqual(connecting.cells, []);
  assert.equal(connecting.status, "checked ago(T)");
});

test("matrix row: no curl there leaves the missing cells empty and says so in the row", () => {
  const harnesses = [row("ready", { name: "claude", label: "Claude Code" }),
                     row("installed", { login_argv: HELD("codex login --device-auth") }),
                     row("missing", { name: "vibe", label: "Vibe", install_command: "curl x | sh" })];
  const r = matrixRow({ name: "build-1", label: "build-1", connected: true, note: "" }, inv(harnesses), true, ago);
  assert.deepEqual(r.cells.map((c) => c.show), ["ready", "sign-in", null, null, null, null]);
  assert.equal(r.status, "needs curl \u00b7 checked ago(T)");
});
