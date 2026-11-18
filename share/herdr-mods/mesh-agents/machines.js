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

// The name rule of cockpit/machines.py (NAME_RE): what hc can only read as a name, never as an option.
export const passable = (text) => typeof text === "string" && /^[A-Za-z0-9][A-Za-z0-9._@:-]{0,253}$/.test(text);

// This Mac first, then Herdr's machines. Only a connected machine whose name passes is asked for its inventory;
// a name that does not pass is listed and never reaches `--on` or `space.new`.
export function machineRows(list) {
  const rows = [{ name: null, label: "This Mac", connected: true, note: "" }];
  for (const m of list?.machines ?? []) {
    const label = plain(m?.name, 254);
    if (!label) continue;
    if (!passable(m.name)) {
      rows.push({ name: label, label, connected: false, note: "name not supported" });
      continue;
    }
    const state = plain(m.state, 40) || "not connected";
    const connected = state === "connected";
    rows.push({ name: m.name, label, connected,
                note: connected ? "" : `${state.replace(/_/g, " ")}: inventory once it is connected` });
  }
  return rows;
}

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
  if (!m.connected) return { ...out, note: m.note };  // never an unsupported name's inventory
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

// `machine add` runs attached in a tab on this Mac (hc and the CLI live here): hc's host key, password and
// 2FA prompts appear there.
export function addHostCommand(spaces, cli, alias) {
  return tabCommand(spaces, null, `Add ${alias}`, [cli, "machine", "add", alias]);
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
