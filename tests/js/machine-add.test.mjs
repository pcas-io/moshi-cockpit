// The add flow of the Machines tab: host key check, trust, add without a terminal, password fallback.
// Run: node --test tests/js/
import { test } from "node:test";
import assert from "node:assert/strict";
import { ADD_EXEC, ADD_NOTE, HOSTKEY_EXEC, IDLE, RECENT_MS, TRUST_EXEC, addBatchArgs, addEnded, addStarted, busy, cleanAdds,
         closedNote, flowView, hostkeyArgs, hostsRemoveArgs, keyLabel, nextStep, recentNames, takeOutcome, trustArgs }
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
  assert.deepEqual(flowView(s), { text: `Adding ${T} and installing herdr-core there. The first connection can take a minute or two.`,
                                  tone: "dim", buttons: [] });
});

test("a known key goes straight to adding; success says so", () => {
  let s = nextStep({ step: "checking", target: T }, { event: "hostkey", target: T, data: { known: true, keys: [] } });
  assert.deepEqual(s, { step: "adding", target: T });
  s = nextStep(s, { event: "added", target: T, data: { ok: true, machine: "build-1", inventory: { harnesses: [] } } });
  assert.deepEqual(s, { step: "idle", note: "build-1 was added." });
  assert.deepEqual(flowView(s), { text: "build-1 was added.", tone: "dim", buttons: [] });
  s = nextStep({ step: "adding", target: T }, { event: "added", target: T, data: { ok: true, machine: null } });
  assert.deepEqual(s, { step: "idle", note: `${T} was added.` });
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

test("an add's outcome waits for the next Machines tab, shown once", () => {
  const t0 = 1_000_000;
  let adds = addStarted({}, T, t0);
  assert.deepEqual(adds, { [T]: { at: t0, pending: true } });
  const ok = nextStep({ step: "adding", target: T }, { event: "added", target: T, data: { ok: true, machine: "build-1" } });
  adds = addEnded(adds, T, ok, "build-1", t0 + 5000);
  assert.deepEqual(adds, { [T]: { at: t0 + 5000, machine: "build-1", outcome: { step: "idle", note: "build-1 was added." } } });
  // what goes to localStorage comes back unchanged; then it is shown once
  adds = cleanAdds(JSON.parse(JSON.stringify(adds)), t0 + 6000);
  let taken = takeOutcome(adds);
  assert.deepEqual(taken.flow, { step: "idle", note: "build-1 was added." });
  assert.deepEqual(taken.adds, { [T]: { at: t0 + 5000, machine: "build-1" } });
  assert.equal(takeOutcome(taken.adds).flow, null);
  // the latest of several, a failure or a step that offers a tab keeps its button
  const failed = { step: "failed", target: "devbox", message: "no route to host" };
  adds = addEnded(addEnded({}, T, { step: "password", target: T }, null, t0), "devbox", failed, null, t0 + 1);
  taken = takeOutcome(adds);
  assert.deepEqual(taken.flow, failed);
  assert.deepEqual(flowView(taken.flow), { text: "no route to host", tone: "blocked", buttons: [] });
  assert.equal(takeOutcome(taken.adds).flow, null);  // all cleared
  // an add whose answer never came (the dialog closed and took it along)
  taken = takeOutcome(addStarted({}, "devbox", t0));
  assert.deepEqual(taken.flow, { step: "idle", note: closedNote("devbox") });
  assert.equal(closedNote("devbox"), "This dialog closed while devbox was being added. The list above shows whether it arrived.");
  assert.deepEqual(taken.adds, { devbox: { at: t0 } });
  // a cancel or a running step leaves no outcome
  assert.deepEqual(addEnded({}, T, IDLE, null, t0), { [T]: { at: t0 } });
});

test("an outcome shows once: in the live dialog, or in the next tab when the dialog closed first", () => {
  const t0 = 1_000_000;
  const ok = { step: "idle", note: "build-1 was added." };
  const store = (adds) => cleanAdds(JSON.parse(JSON.stringify(adds)), t0 + 10000);  // through localStorage
  // shown live: the pending mark goes, nothing is left to show again; the time stays for "Connecting"
  let adds = store(addEnded(addStarted({}, T, t0), T, ok, "build-1", t0 + 5000, true));
  assert.deepEqual(adds, { [T]: { at: t0 + 5000, machine: "build-1" } });
  assert.equal(takeOutcome(adds).flow, null);
  // closed before the answer, which never came: the pending mark shows once, then nothing
  let taken = takeOutcome(store(addStarted({}, T, t0)));
  assert.deepEqual(taken.flow, { step: "idle", note: closedNote(T) });
  assert.equal(takeOutcome(store(taken.adds)).flow, null);
  // closed before the answer, which came late: its outcome shows once, then nothing
  taken = takeOutcome(store(addEnded(addStarted({}, T, t0), T, ok, "build-1", t0 + 5000, false)));
  assert.deepEqual(taken.flow, ok);
  assert.equal(takeOutcome(store(taken.adds)).flow, null);
});

test("stored adds are checked before they count and expire", () => {
  const t0 = 1_000_000;
  const raw = { "-oProxyCommand=x": { at: t0 }, "a b": { at: t0 }, devbox: { at: "x" }, old: { at: t0 - 600000 },
                future: { at: t0 + 1 }, [T]: { at: t0, machine: "-x", pending: "yes", outcome: { step: "adding", target: T } },
                "build-1": { at: t0, outcome: { step: "failed", target: "-evil", message: "a\u001b[2Jb" } } };
  assert.deepEqual(cleanAdds(raw, t0), { [T]: { at: t0 }, "build-1": { at: t0, outcome: { step: "failed", target: "build-1", message: "a [2Jb" } } });
  for (const junk of [null, "x", [], 3]) assert.deepEqual(cleanAdds(junk, t0), {});
});

test("a machine added a moment ago is recent, by hc's name or by target", () => {
  const t0 = 1_000_000;
  const list = { machines: [{ name: "build-1", target: "deploy@build-1" }, { name: "devbox", target: "me@devbox" },
                            { name: "lab-2", target: "lab-2" }, { name: "-x", target: "x@y" }] };
  const adds = { [T]: { at: t0 }, "me@devbox": { at: t0, machine: "devbox" }, "lab-2": { at: t0 - RECENT_MS } };
  assert.deepEqual([...recentNames(adds, list, t0 + 1000)].sort(), ["build-1", "devbox"]);
  assert.deepEqual([...recentNames(adds, list, t0 + RECENT_MS)], []);
  // still running: counts from its start until its answer is due, plus the connecting time
  const running = { "x@y": { at: t0, pending: true }, [T]: { at: t0, pending: true } };
  assert.deepEqual([...recentNames(running, list, t0 + ADD_EXEC.timeout + RECENT_MS - 1)], ["build-1"]);
  assert.deepEqual([...recentNames(running, list, t0 + ADD_EXEC.timeout + RECENT_MS)], []);
  assert.deepEqual([...recentNames(null, list, t0)], []);
});
