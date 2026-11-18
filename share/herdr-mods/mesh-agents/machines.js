// "+ Agent" → Machines: what the tab makes of `machine list --json` and `machine inventory --json`, and the tabs
// its buttons open. Pure, so `node --test` can load it (tests/js/machines.test.mjs); the view is machines-view.js.

import { plain } from "./rules.js";

export const STATE_TEXT = { ready: "Ready", installed: "Installed, not signed in", missing: "Missing" };

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

// A harness row's button. Installing on another machine asks here first (the command runs there as the ssh
// user); on this Mac `moshi-cockpit harness install` asks y/N in its own tab.
export function harnessAction(row, machine) {
  const none = { kind: null, label: "", argv: null, confirm: null, note: "" };
  if (row?.state === "installed") {
    return { ...none, kind: "sign-in", label: "Sign in", argv: row.login_argv ?? row.start_argv };
  }
  if (row?.state !== "missing") return none;
  if (!row.install_argv) return machine ? { ...none, note: `needs curl on ${machine}` } : none;
  const confirm = machine ? `Runs ${row.install_command} on ${machine} as your SSH user, in a new tab.` : null;
  return { ...none, kind: "install", label: "Install", argv: row.install_argv, confirm };
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
