// The add flow of the Machines tab: host key check, trust, add without a terminal, password fallback.
// Run: node --test tests/js/
import { test } from "node:test";
import assert from "node:assert/strict";
import { ADD_EXEC, ADD_NOTE, HOSTKEY_EXEC, IDLE, TRUST_EXEC, addBatchArgs, busy, flowView, hostkeyArgs, hostsRemoveArgs,
         keyLabel, nextStep, trustArgs }
  from "../../share/herdr-mods/mesh-agents/machines.js";
import { readFileSync } from "node:fs";

const ED = { type: "ssh-ed25519", sha256: "SHA256:AlQFAzJ5rO26Lgs7fs+OsYg0HFG32J+2ZmesKFP0184" };
const RSA = { type: "ssh-rsa", sha256: "SHA256:" + "A".repeat(43) };
const T = "deploy@build-1";

test("the CLI calls of the flow", () => {
  assert.deepEqual(hostkeyArgs(T), ["machine", "hostkey", T, "--json"]);
  assert.deepEqual(trustArgs(T, ED), ["machine", "trust", T, "--type", "ssh-ed25519", "--fingerprint", ED.sha256, "--json"]);
  assert.deepEqual(addBatchArgs(T), ["machine", "add", T, "--batch", "--json"]);
  assert.deepEqual(hostsRemoveArgs(T), ["machine", "hosts", "remove", T]);
  // exec waits longer than the CLI's worst case, read from the CLI's own constants
  const py = (file) => readFileSync(new URL(`../../cockpit/${file}`, import.meta.url), "utf8");
  const num = (source, name) => Number(source.match(new RegExp(`^${name} = (\\d+)`, "m"))[1]);
  const m = py("machines.py");
  const k = py("hostkeys.py");
  const addWorst = 2 * num(m, "LIST_TIMEOUT") + num(m, "ADD_TIMEOUT") + num(m, "PROBE_TIMEOUT");
  assert.equal(addWorst, 195);
  assert.ok(ADD_EXEC.timeout > addWorst * 1000, `${ADD_EXEC.timeout} > ${addWorst * 1000}`);
  assert.ok(HOSTKEY_EXEC.timeout > (2 * num(k, "LOCAL_TIMEOUT") + num(k, "SCAN_TIMEOUT")) * 1000);
  assert.ok(TRUST_EXEC.timeout > (num(k, "LOCAL_TIMEOUT") + num(k, "SCAN_TIMEOUT")) * 1000);
  for (const file of ["machines-view.js"]) {
    const source = readFileSync(new URL(`../../share/herdr-mods/mesh-agents/${file}`, import.meta.url), "utf8");
    assert.match(source, /addBatchArgs\([^)]*\), true, ADD_EXEC\)/);
    assert.match(source, /hostkeyArgs\([^)]*\), true, HOSTKEY_EXEC\)/);
    assert.match(source, /trustArgs\([^)]*\), true, TRUST_EXEC\)/);
  }
});

test("key labels", () => {
  assert.equal(keyLabel("ssh-ed25519"), "ED25519");
  assert.equal(keyLabel("ecdsa-sha2-nistp256"), "ECDSA");
  assert.equal(keyLabel("ssh-rsa"), "RSA");
  assert.equal(keyLabel("ssh-x\u001b[31m"), "SSH-X [31M");
});

test("an unknown key asks first; Trust adds, Cancel ends", () => {
  let s = nextStep(IDLE, { event: "start", target: T });
  assert.deepEqual(s, { step: "checking", target: T });
  assert.ok(busy(s));
  assert.deepEqual(flowView(s), { text: `Checking ${T}…`, tone: "dim", buttons: [] });
  s = nextStep(s, { event: "hostkey", target: T, data: { known: false, keys: [ED, RSA] } });
  assert.deepEqual(s, { step: "trust", target: T, key: ED });
  assert.ok(!busy(s));
  assert.deepEqual(flowView(s), { text: `Trust ${T}? ED25519 ${ED.sha256}`, tone: "foreground", buttons: ["trust", "cancel"] });
  assert.deepEqual(nextStep(s, { event: "cancel" }), IDLE);
  s = nextStep(s, { event: "trust", target: T });
  assert.deepEqual(s, { step: "trusting", target: T, key: ED });
  assert.ok(busy(s));
  s = nextStep(s, { event: "trusted", target: T });
  assert.deepEqual(s, { step: "adding", target: T });
  assert.deepEqual(flowView(s), { text: `Adding ${T} and installing herdr-core there…`, tone: "dim", buttons: [] });
});

test("a known key goes straight to adding; success clears the flow", () => {
  let s = nextStep({ step: "checking", target: T }, { event: "hostkey", target: T, data: { known: true, keys: [] } });
  assert.deepEqual(s, { step: "adding", target: T });
  s = nextStep(s, { event: "added", target: T, data: { ok: true, machine: "build-1", inventory: { harnesses: [] } } });
  assert.deepEqual(s, IDLE);
  assert.equal(flowView(s), null);
  s = nextStep({ step: "adding", target: T }, { event: "added", target: T,
                                                  data: { ok: true, machine: null, message: `${T} was added; Herdr does not list it yet.` } });
  assert.deepEqual(s, { step: "idle", note: `${T} was added; Herdr does not list it yet.` });
  assert.deepEqual(flowView(s), { text: `${T} was added; Herdr does not list it yet.`, tone: "dim", buttons: [] });
});

test("a password goes to a tab; other failures show inline", () => {
  const adding = { step: "adding", target: T };
  let s = nextStep(adding, { event: "added", target: T, data: { ok: false, needs: "password", message: "logging in needs a terminal" } });
  assert.deepEqual(s, { step: "password", target: T });
  assert.deepEqual(flowView(s), { text: `${T} asks for a password. Set up an ssh key for it, or answer in a tab.`,
                                  tone: "foreground", buttons: ["tab"] });
  s = nextStep(adding, { event: "added", target: T, data: { ok: false, message: "no route\nto host" } });
  assert.deepEqual(s, { step: "failed", target: T, message: "no route to host" });
  assert.deepEqual(flowView(s), { text: "no route to host", tone: "blocked", buttons: [] });
  s = nextStep(adding, { event: "added", target: T, data: { ok: false, needs: "host_key", message: "Host key verification failed." } });
  assert.deepEqual(s, { step: "failed", target: T, message: "Host key verification failed." });
  s = nextStep(adding, { event: "added", target: T, data: null });
  assert.equal(s.step, "failed");
  assert.ok(s.message);
  for (const busyState of [{ step: "checking", target: T }, { step: "trusting", target: T, key: ED }, adding]) {
    assert.deepEqual(nextStep(busyState, { event: "error", target: T, message: "devbox did not answer: timeout" }),
                     { step: "failed", target: T, message: "devbox did not answer: timeout" });
  }
});

test("keys only, a login hc can't do or ssh settings the dialog doesn't handle offer a tab", () => {
  const msg = `${T} uses ssh settings this dialog doesn't handle (ProxyJump); add it in a tab.`;
  let s = nextStep({ step: "checking", target: T }, { event: "hostkey", target: T, data: { target: T, needs: "tab", message: msg } });
  assert.deepEqual(s, { step: "tab", target: T, message: msg });
  assert.ok(!busy(s));
  assert.deepEqual(flowView(s), { text: msg, tone: "foreground", buttons: ["tab"] });
  const key = `${T} accepts only ssh keys and none of yours worked.`;
  s = nextStep({ step: "adding", target: T }, { event: "added", target: T, data: { ok: false, needs: "key", message: key } });
  assert.deepEqual(flowView(s), { text: key, tone: "foreground", buttons: ["tab"] });
  s = nextStep({ step: "adding", target: T }, { event: "added", target: T, data: { ok: false, needs: "tab", message: "" } });
  assert.deepEqual(s, { step: "tab", target: T, message: `Add ${T} in a tab.` });
});

test("a host key answer without a usable key fails; odd keys never reach trust", () => {
  const checking = { step: "checking", target: T };
  for (const keys of [[], null, [{ type: "-x", sha256: ED.sha256 }], [{ type: "ssh-ed25519", sha256: "MD5:aa" }]]) {
    const s = nextStep(checking, { event: "hostkey", target: T, data: { known: false, keys } });
    assert.equal(s.step, "failed", JSON.stringify(keys));
    assert.equal(s.message, `${T} offered no host key`);
  }
});

test("answers for another step or target change nothing", () => {
  const trust = { step: "trust", target: T, key: ED };
  assert.equal(nextStep(trust, { event: "added", target: T, data: { ok: true } }), trust);
  assert.equal(nextStep(trust, { event: "trusted", target: T }), trust);
  const checking = { step: "checking", target: T };
  assert.equal(nextStep(checking, { event: "hostkey", target: "devbox", data: { known: true } }), checking);
  assert.equal(nextStep(IDLE, { event: "error", target: T, message: "x" }), IDLE);
  // a new start is refused while one is running, and allowed from any other step
  assert.equal(nextStep(checking, { event: "start", target: "devbox" }), checking);
  for (const s of [IDLE, trust, { step: "password", target: T }, { step: "failed", target: T, message: "x" }]) {
    assert.deepEqual(nextStep(s, { event: "start", target: "devbox" }), { step: "checking", target: "devbox" });
  }
});

test("the note under the grid", () => {
  assert.equal(ADD_NOTE, "Herdr's own \"Add a machine\" can't answer ssh's questions (host key, password, installing herdr-core). "
                         + "Add machines here.");
});
