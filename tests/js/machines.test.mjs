// The Machines tab of "+ Agent": machine rows, harness actions and the tabs they open. Run: node --test tests/js/
import { test } from "node:test";
import assert from "node:assert/strict";
import { STATE_TEXT, addHostCommand, harnessAction, hostRows, inventoryArgs, machineRows, passable, tabCommand }
  from "../../share/herdr-mods/mesh-agents/machines.js";

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
