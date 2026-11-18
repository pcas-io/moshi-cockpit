// "+ Agent" → Machines: what the tab makes of `machine list --json` and `machine inventory --json`, and the tabs
// its cells and chips open; "+ Agent" → New on another machine. Pure, so `node --test` can load it
// (tests/js/machines.test.mjs); the views are machines-view.js and main.js.

import { plain } from "./rules.js";
import { HERDR_APP } from "./harnesses.js";

// Our six harnesses → Herdr's program key in knownAgents() (opencode is "opencode" there, read from a real
// herdr-app's knownAgents() answer).
export const HERDR_KEYS = { ...HERDR_APP, opencode: "opencode" };

export const STATE_TEXT = { ready: "Ready", installed: "Installed, not signed in", missing: "Missing" };

// exec's options for `machine inventory`: longer than the CLI's own 45 s guard (PROBE_TIMEOUT in
// cockpit/machines.py), so a slow machine ends in the CLI's "<machine> did not answer: …", not in exec's timeout.
export const INVENTORY_EXEC = { timeout: 60000 };

export function inventoryArgs(machine, refresh) {
  return ["machine", "inventory", ...(machine ? ["--on", machine] : []), ...(refresh ? ["--refresh"] : []), "--json"];
}

// The name rule of cockpit/targets.py (NAME_RE): what hc can only read as a name, never as an option.
export const passable = (text) => typeof text === "string" && /^[A-Za-z0-9][A-Za-z0-9._@:-]{0,253}$/.test(text);

// What a machine added a moment ago shows until Herdr has connected to it.
export const connectingText = (name) => `Connecting: Herdr is starting herdr-core on ${name}. This can take a minute or two; `
  + "the row fills in by itself once it answers.";

// This Mac first, then Herdr's machines. Only a connected machine whose name passes is asked for its inventory;
// a name that does not pass is listed and never reaches `--on`, `space.new` or `machine remove`. `recent`: names of
// machines added a moment ago (recentNames): not connected yet, they say "Connecting" instead of their state.
export function machineRows(list, recent = null) {
  const rows = [{ name: null, label: "This Mac", connected: true, note: "", removable: false, connecting: false }];
  for (const m of list?.machines ?? []) {
    const label = plain(m?.name, 254);
    if (!label) continue;
    if (!passable(m.name)) {
      rows.push({ name: label, label, connected: false, note: "name not supported", removable: false, connecting: false });
      continue;
    }
    const state = plain(m.state, 40) || "not connected";
    const connected = state === "connected";
    const connecting = !connected && !!recent?.has(m.name);
    const note = connected ? "" : connecting ? connectingText(label) : `${state.replace(/_/g, " ")}: inventory once it is connected`;
    rows.push({ name: m.name, label, connected, note, removable: true, connecting });
  }
  return rows;
}

// "×" at a machine's row end: Herdr forgets it (`hc machine rm`), its terminals keep running there; the saved host
// stays, so its chip can add it again.
export const removeQuestion = (name) => `Remove ${plain(name, 254)} from Herdr? Its terminals keep running there.`;
export const machineRemoveArgs = (name) => ["machine", "remove", name, "--json"];
export const REMOVE_EXEC = { timeout: 40000 };  // longer than the CLI's 30 s guard (REMOVE_TIMEOUT)

// The ssh hosts not added yet: the raw alias is checked, the label is only for display.
export function hostRows(hosts) {
  return (Array.isArray(hosts) ? hosts : []).filter((h) => typeof h === "string" && plain(h, 254)).map((alias) => {
    const ok = passable(alias);
    return { alias, label: plain(alias, 254), ok, note: ok ? "" : "can't be added from here" };
  });
}

// Herdr opens a tab on another machine only once it has read that machine's layout (machines() says "ready");
// this Mac always can.
export function layoutReady(ms, machine) {
  if (machine == null) return true;
  return (ms ?? []).some((m) => m?.name === machine && m?.layout === "ready");
}

export const connectingNote = (machine) => `Herdr is still connecting to ${machine}`;

// A harness's action (what its matrix cell does). Installing on another machine asks here first (the command runs
// there as the ssh user); on this Mac `moshi-cockpit harness install` asks y/N in its own tab. `ready`:
// layoutReady() for that machine; until then no action (the row shows connectingNote instead of its cells).
export function harnessAction(row, machine, ready = true) {
  const none = { kind: null, label: "", argv: null, confirm: null, note: "" };
  if (machine && !ready) return none;
  if (row?.state === "installed") {
    return { ...none, kind: "sign-in", label: "Sign in", argv: row.login_argv ?? row.start_argv };
  }
  if (row?.state !== "missing") return none;
  if (!row.install_argv) return machine ? { ...none, note: `needs curl on ${machine}` } : none;
  const confirm = machine ? `Runs ${row.install_command} on ${machine} as your SSH user, in a new tab.` : null;
  return { ...none, kind: "install", label: "Install", argv: row.install_argv, confirm };
}

// The Machines matrix: one column per harness, in this order, with a label short enough for a narrow column.
export const COLUMNS = [["claude", "Claude"], ["codex", "Codex"], ["vibe", "Vibe"], ["agy", "Agy"], ["grok", "Grok"],
                        ["opencode", "OpenCode"]].map(([name, short]) => ({ name, short }));

// What a cell shows, and the legend under the tab's buttons.
export const GLYPH = { ready: "\u25cf", "sign-in": "\u25d0", install: "\u25cb" };
export const LEGEND = [{ show: "ready", text: "Ready" }, { show: "sign-in", text: "Sign in" }, { show: "install", text: "Install" }];

// One cell: `show` is "ready", "sign-in", "install" or null (empty); `action` is harnessAction's, unchanged.
// Cells are only drawn once Herdr has the machine's layout (matrixRow), so the action is asked as ready.
export function cellAction(h, machine) {
  const action = harnessAction(h, machine, true);
  const label = plain(h?.label, 40);
  if (h?.state === "ready") return { show: "ready", action, hint: `${label}: ${STATE_TEXT.ready}` };
  if (action.kind === "sign-in") return { show: "sign-in", action, hint: `${label}: ${STATE_TEXT.installed}. Click to sign in.` };
  if (action.kind === "install") return { show: "install", action, hint: `${label}: ${STATE_TEXT.missing}. Click to install.` };
  return { show: null, action, hint: action.note ? `${label}: ${STATE_TEXT.missing}, ${action.note}` : "" };
}

// One machine's row of the matrix. `inv`: the view's {loading, data, error} for it; `ready`: layoutReady() for it;
// `ago`: formats checked_at. A note or an error replaces the cells; a refresh in flight keeps the last answer.
export function matrixRow(m, inv, ready, ago) {
  const out = { label: m.label, status: "", note: "", error: "", refresh: !!m.connected, loading: false, cells: [] };
  if (!m.connected) return { ...out, note: m.note, refresh: !!m.connecting };  // never an unsupported name's inventory
  const data = Array.isArray(inv?.data?.harnesses) ? inv.data : null;
  const checked = data ? `checked ${ago(data.checked_at)}` : "";
  if (inv?.loading) Object.assign(out, { loading: true, status: "Checking\u2026" });
  if (inv?.error) return { ...out, error: inv.error };
  if (!data) return out;
  if (!out.loading) out.status = checked;
  if (!ready) return { ...out, note: connectingNote(m.label) };
  out.cells = COLUMNS.map((c) => ({ ...c, ...cellAction(data.harnesses.find((h) => h?.name === c.name), m.name) }));
  if (!out.loading && out.cells.some((c) => c.action.note)) out.status = `needs curl \u00b7 ${checked}`;
  return out;
}

// Where the tab opens: in the first space on that machine (this Mac: a space without a machine), else in a new
// space there. A local command never lands in a remote space.
export function tabCommand(spaces, machine, name, argv) {
  const space = (spaces ?? []).find((s) => (machine ? s.machine === machine : s.machine == null));
  if (space) return { command: "tab.new", args: { name, argv, space: space.id } };
  return { command: "space.new", args: machine ? { machine, name, argv } : { name, argv } };
}

// The password fallback ("In a tab"): `machine add` attached in a tab on this Mac (hc and the CLI live here), where
// hc's password and 2FA prompts appear.
export function addHostCommand(spaces, cli, alias) {
  return tabCommand(spaces, null, `Add ${alias}`, [cli, "machine", "add", alias]);
}

// "Add a machine": a target typed under the chips (user@host, user@host:port or an ssh alias). Trimmed, then held
// to the chips' rule; only `ok` starts the add flow, otherwise `note` shows and nothing runs.
export function typedTarget(text) {
  const value = typeof text === "string" ? text.trim() : "";
  return passable(value) ? { ok: true, value, note: "" } : { ok: false, value, note: "Not a target ssh can use" };
}

export const ADD_NOTE = "Herdr's own \"Add a machine\" can't answer ssh's questions (host key, password, installing "
  + "herdr-core). Add machines here.";

// The add flow's CLI calls; exec waits longer than the CLI's worst case (tests/js/machine-add.test.mjs reads the
// guards from cockpit/machines.py and cockpit/hostkeys.py): add --batch is machine ls 15 s + hc machine add 120 s +
// machine ls 15 s + the probe 45 s = 195 s; hostkey ssh -G 5 + ssh-keygen 5 + ssh-keyscan 12 s; trust 5 + 12 s.
export const HOSTKEY_EXEC = { timeout: 30000 };
export const TRUST_EXEC = { timeout: 30000 };
export const ADD_EXEC = { timeout: 210000 };
export const hostkeyArgs = (target) => ["machine", "hostkey", target, "--json"];
export const trustArgs = (target, key) => ["machine", "trust", target, "--type", key.type, "--fingerprint", key.sha256, "--json"];
export const addBatchArgs = (target) => ["machine", "add", target, "--batch", "--json"];
export const hostsRemoveArgs = (target) => ["machine", "hosts", "remove", target];

// What `ssh-keygen -l` calls the key type.
export function keyLabel(type) {
  const text = plain(type, 64);
  return /ed25519/.test(text) ? "ED25519" : /ecdsa/.test(text) ? "ECDSA" : /rsa/.test(text) ? "RSA" : text.toUpperCase();
}

// Only a key of this shape reaches `machine trust`'s argv (the CLI checks it again).
const usableKey = (k) => typeof k?.type === "string" && /^[a-z0-9][a-z0-9@.-]{0,63}$/.test(k.type)
  && typeof k?.sha256 === "string" && /^SHA256:[A-Za-z0-9+/]{43}$/.test(k.sha256);

// The add flow, one area under the field. Steps: idle (an optional note), checking (`machine hostkey`), trust (the
// first offered key, waiting for Trust or Cancel), trusting (`machine trust`), adding (`machine add --batch`),
// password and tab (both offer "In a tab": a password, keys only, or ssh settings the dialog doesn't handle),
// failed (the message). `answer`: {event, target, data | message}; one for another
// step or target (a late answer after Cancel) changes nothing, and a start waits until a running step ends.
export const IDLE = { step: "idle" };
const RUNNING = ["checking", "trusting", "adding"];
export const busy = (state) => RUNNING.includes(state?.step);

export function nextStep(state, answer) {
  const event = answer?.event;
  if (event === "start") return busy(state) ? state : { step: "checking", target: answer.target };
  if (event === "cancel") return IDLE;
  if (answer?.target !== state?.target) return state;
  const failed = (message) => ({ step: "failed", target: state.target, message: plain(message, 300) || "moshi-cockpit gave no answer" });
  if (event === "error" && busy(state)) return failed(answer.message);
  const inTab = (message) => ({ step: "tab", target: state.target, message: plain(message, 300) || `Add ${state.target} in a tab.` });
  if (event === "hostkey" && state.step === "checking") {
    if (answer.data?.needs === "tab") return inTab(answer.data.message);
    if (answer.data?.known === true) return { step: "adding", target: state.target };
    const key = Array.isArray(answer.data?.keys) ? answer.data.keys[0] : null;
    return usableKey(key) ? { step: "trust", target: state.target, key: { type: key.type, sha256: key.sha256 } }
      : failed(`${state.target} offered no host key`);
  }
  if (event === "trust" && state.step === "trust") return { ...state, step: "trusting" };
  if (event === "trusted" && state.step === "trusting") return { step: "adding", target: state.target };
  if (event === "added" && state.step === "adding") {
    const data = answer.data;
    if (data?.ok === true) return { step: "idle", note: plain(data.message, 300) || `${plain(data.machine, 254) || state.target} was added.` };
    if (data?.needs === "password") return { step: "password", target: state.target };
    if (data?.needs === "key" || data?.needs === "tab") return inTab(data.message);
    return failed(data?.message);
  }
  return state;
}

// What the flow area shows: a line, its tone and the buttons ("trust", "cancel", "tab"); null for nothing.
export function flowView(state) {
  const line = (text, tone = "dim", buttons = []) => ({ text, tone, buttons });
  const target = plain(state?.target, 254);
  switch (state?.step) {
    case "checking": case "trusting": return line(`Checking ${target}\u2026`);
    case "trust": return line(`Trust ${target}? ${keyLabel(state.key.type)} ${state.key.sha256}`, "foreground", ["trust", "cancel"]);
    case "adding": return line(`Adding ${target} and installing herdr-core there. The first connection can take a minute or two.`);
    case "password": return line(`${target} asks for a password. Set up an ssh key for it, or answer in a tab.`, "foreground", ["tab"]);
    case "tab": return line(state.message, "foreground", ["tab"]);
    case "failed": return line(state.message, "blocked");
    default: return state?.note ? line(state.note) : null;
  }
}

// Herdr may close the dialog while an add runs. The flow keeps a mark per target in the mod's localStorage
// (ADDS_KEY; targets, machine names and the outcome line only): {target: {at, pending?, machine?, outcome?}}. A mark
// is pending while the add is unanswered; an answer the live dialog shows replaces it with time and machine only,
// an answer that arrives after the dialog closed stores its outcome. The next Machines tab shows the latest outcome,
// or that an add never answered, once (takeOutcome). An entry goes KEEP_MS after its last write; for RECENT_MS after
// an add the machine shows "Connecting" (recentNames).
export const ADDS_KEY = "machines.adds";
export const RECENT_MS = 120000;
const KEEP_MS = 600000;
const SHOWN = ["idle", "failed", "password", "tab"];

export const closedNote = (target) => `This dialog closed while ${target} was being added. The list above shows whether it arrived.`;

// Only what this file wrote comes back: a passable target, a time, a machine name, an outcome of a step that waits.
function cleanOutcome(o, target) {
  if (!SHOWN.includes(o?.step)) return null;
  if (o.step === "idle") return plain(o.note, 300) ? { step: "idle", note: plain(o.note, 300) } : null;
  if (o.step === "password") return { step: "password", target };
  return { step: o.step, target, message: plain(o.message, 300) || (o.step === "tab" ? `Add ${target} in a tab.` : "not added") };
}

export function cleanAdds(raw, now) {
  const out = {};
  if (!raw || typeof raw !== "object" || Array.isArray(raw)) return out;
  for (const [target, e] of Object.entries(raw)) {
    if (!passable(target) || typeof e?.at !== "number" || !(now - e.at >= 0 && now - e.at < KEEP_MS)) continue;
    const entry = { at: e.at };
    if (e.pending === true) entry.pending = true;
    if (passable(e.machine)) entry.machine = e.machine;
    const outcome = cleanOutcome(e.outcome, target);
    if (outcome) entry.outcome = outcome;
    out[target] = entry;
  }
  return out;
}

export const addStarted = (adds, target, now) => ({ ...adds, [target]: { at: now, pending: true } });

// `flow`: the step the answer led to; `machine`: the name hc gave it, when the add worked; `shown`: the live dialog
// shows the answer, so nothing waits for the next one.
export function addEnded(adds, target, flow, machine, now, shown = false) {
  const entry = { at: now };
  if (passable(machine)) entry.machine = machine;
  const outcome = shown ? null : cleanOutcome(flow, target);
  if (outcome) entry.outcome = outcome;
  return { ...adds, [target]: entry };
}

// The latest outcome (or an add that never answered) as a flow step to show, and the entries without any.
export function takeOutcome(adds) {
  let latest = null;
  for (const [target, e] of Object.entries(adds)) {
    if ((e.outcome || e.pending) && (!latest || e.at > latest.e.at)) latest = { target, e };
  }
  const rest = Object.fromEntries(Object.entries(adds).map(([target, e]) => {
    const { outcome: _o, pending: _p, ...kept } = e;
    return [target, kept];
  }));
  if (!latest) return { flow: null, adds: rest };
  const flow = latest.e.outcome ?? { step: "idle", note: closedNote(latest.target) };
  return { flow, adds: rest };
}

// Herdr's machines (`machine list`'s rows) added within RECENT_MS, by the name hc gave, or by target. An add still
// running (pending: its time is the start) counts until its answer is due, plus RECENT_MS.
export function recentNames(adds, list, now) {
  const names = new Set();
  for (const [target, e] of Object.entries(adds ?? {})) {
    if (!(now - e.at < (e.pending ? ADD_EXEC.timeout : 0) + RECENT_MS)) continue;
    for (const m of list?.machines ?? []) {
      if (passable(m?.name) && (m.name === e.machine || m.name === target || m.target === target)) names.add(m.name);
    }
  }
  return names;
}

// "+ Agent" → New: where the agent starts. This Mac, then each machine whose layout Herdr has read; a name hc
// could read as an option never reaches knownAgents({on}) or space.new {machine}.
export function whereChoices(ms) {
  const remote = (ms ?? []).filter((m) => m?.name != null && m.layout === "ready" && passable(m.name));
  return [{ name: null, label: "This Mac" }, ...remote.map((m) => ({ name: m.name, label: m.name }))];
}

// Herdr's own check on that machine (knownAgents({on}) → `known`) finds this harness with a command to run it.
function herdrRun(row, known) {
  const app = (Array.isArray(known) ? known : []).find((a) => a?.key === HERDR_KEYS[row?.name]);
  return app?.installed && Array.isArray(app.run) && app.run.length > 0 ? app.run : null;
}

// The harness chips for another machine, from its inventory rows: ready and installed (the first start signs
// in) can start, and so can one Herdr's own check finds there (`known`); the rest of missing is explained.
export function remoteChips(rows, machine, known = null) {
  return (Array.isArray(rows) ? rows : []).filter((r) => Object.prototype.hasOwnProperty.call(HERDR_KEYS, r?.name)).map((r) => {
    const selectable = r.state === "ready" || r.state === "installed" || herdrRun(r, known) !== null;
    return { name: r.name, label: plain(r.label, 40), selectable,
             note: r.state === "missing" && !selectable ? `missing on ${machine}: install it under Machines` : "" };
  });
}

// space.new's arguments: Herdr's own command when its check on that machine finds the harness, else the
// inventory's start command. Nothing goes to moshi or agents.json.
export function remoteStart(machine, row, known) {
  const argv = herdrRun(row, known) ?? row.start_argv;
  return { machine, name: plain(row.label, 40), argv };
}
