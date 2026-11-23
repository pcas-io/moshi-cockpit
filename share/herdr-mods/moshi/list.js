// What "Your agents" shows, free of herdr so the tests can run it.

const ORDER = { live: 0, stale: 1, offline: 2, never: 3 };

// Control characters (escapes, BEL, CR/LF) out of any text that came from a server or a process.
export function plain(text, max = 300) {
  return String(text ?? "").replace(/[\u0000-\u001f\u007f-\u009f]+/g, " ").trim().slice(0, max);
}

// The name rule of mesh-agents' machines.js (`passable`): only such a machine name reaches space.new {machine}.
const passable = (text) => typeof text === "string" && /^[A-Za-z0-9][A-Za-z0-9._@:-]{0,253}$/.test(text);

// The person's own agents, each with its presence in moshi. Only an agent with its own moshi identity has
// one: a harness that reaches moshi through the account would show the account's presence as its own.
// `remote`: the agents of Linux machines (status --json, from the cache), each with its own moshi identity. A name
// that is also local, or on an earlier machine, shows once: the same identity, and the takeover decides where it runs.
// `listed`: Herdr's machines (machines()); a remote agent shows only while Herdr still lists its machine.
export function ownAgents(local, mesh, remote = [], listed = []) {
  const byName = new Map((mesh ?? []).map((a) => [String(a.name ?? "").toLowerCase(), a]));
  const presence = (found) => ({ presence: found?.presence ?? null, working_on: found?.working_on ?? "",
                                 last_seen_at: found?.last_seen_at ?? "" });
  const rows = (local ?? []).map((a) => ({
    name: a.name, harness: a.harness, role: a.role ?? "", installed: !!a.installed, machine: null,
    ...presence(a.has_moshi_token ? byName.get(String(a.name).toLowerCase()) : undefined) }));
  const seen = new Set(rows.map((a) => String(a.name).toLowerCase()));
  const machines = new Set((listed ?? []).map((m) => m?.name).filter((name) => typeof name === "string" && name));
  for (const a of remote ?? []) {
    const key = String(a?.name ?? "").toLowerCase();
    if (!key || seen.has(key) || !machines.has(a.machine)) continue;
    seen.add(key);
    rows.push({ name: a.name, harness: a.harness, role: a.role ?? "", installed: true, machine: plain(a.machine, 254),
                runArgv: a.run_argv, ...presence(byName.get(key)) });
  }
  return rows.sort((a, b) => (ORDER[a.presence] ?? 9) - (ORDER[b.presence] ?? 9) || a.name.localeCompare(b.name));
}

// Starting a remote agent: a tab in the first space on its machine, else a new space there. null for a machine name
// that does not pass or a row without the command to run.
export function remoteCommand(spaces, a) {
  if (!passable(a?.machine) || !Array.isArray(a.runArgv) || !a.runArgv.length) return null;
  const space = (spaces ?? []).find((s) => s.machine === a.machine);
  if (space) return { command: "tab.new", args: { name: a.name, argv: a.runArgv, space: space.id } };
  return { command: "space.new", args: { machine: a.machine, name: a.name, argv: a.runArgv } };
}

// The terminal already running this agent, if any: started from our menu or dialog, its tab carries the
// agent's name (with the menu's note in brackets at most). No match by CLI: a plain codex tab is not this agent.
export function runningTerminal(name, terminals) {
  const own = (terminals ?? []).filter((a) => a.terminal !== null && a.address
    && ((a.tab_name ?? "") === name || (a.tab_name ?? "").startsWith(`${name} (`)));
  return own.sort((a, b) => (b.seq ?? 0) - (a.seq ?? 0))[0]?.address ?? null;
}

// The one line about the connection to moshi, or null while all is well. Never a dialog: a revoked device key
// or a moshi out of reach must not pop up anything. `connect`: a click opens the connect dialog.
export function sidebarLine(data) {
  if (data?.disconnected) return { text: "Disconnected from moshi — Connect again", connect: true };
  if (data?.unreachable) return { text: "moshi not reachable", connect: false };
  if (data?.needs_connect) return { text: "Not connected to moshi", connect: true };
  return null;
}
