// What "Your agents" shows, free of herdr so the tests can run it.

const ORDER = { live: 0, stale: 1, offline: 2, never: 3 };

// The person's own agents, each with its presence in moshi. Only an agent with its own moshi identity has
// one: a harness that reaches moshi through the account would show the account's presence as its own.
export function ownAgents(local, mesh) {
  const byName = new Map((mesh ?? []).map((a) => [String(a.name ?? "").toLowerCase(), a]));
  return (local ?? [])
    .map((a) => {
      const found = a.has_moshi_token ? byName.get(String(a.name).toLowerCase()) : undefined;
      return { name: a.name, harness: a.harness, role: a.role ?? "", installed: !!a.installed,
               presence: found?.presence ?? null, working_on: found?.working_on ?? "",
               last_seen_at: found?.last_seen_at ?? "" };
    })
    .sort((a, b) => (ORDER[a.presence] ?? 9) - (ORDER[b.presence] ?? 9) || a.name.localeCompare(b.name));
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
