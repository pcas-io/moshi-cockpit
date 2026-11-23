// moshi-cockpit on another machine, from "+ Agent": what the Machines tab, New and From moshi make of
// `machine cockpit --json` and `machine agent … --json` (cockpit/remote.py). Pure, so `node --test` can load it
// (tests/js/remote.test.mjs); the views are machines-view.js and main.js.

import { plain } from "./rules.js";
import { NAMES } from "./harnesses.js";

// exec's options: longer than the CLI's own guards (45 s for the state, 120 s for an agent call), so a slow
// machine ends in the CLI's "<machine> did not answer: …", not in exec's timeout.
export const COCKPIT_EXEC = { timeout: 60000 };
export const AGENT_EXEC = { timeout: 180000 };  // the CLI: add/load up to 120 s, then one re-probe up to 45 s

export function cockpitArgs(machine, refresh) {
  return ["machine", "cockpit", "--on", machine, ...(refresh ? ["--refresh"] : []), "--json"];
}

const argvOf = (argv) => (Array.isArray(argv) && argv.length > 0 && argv.every((a) => typeof a === "string") ? argv : null);

// The link beside a machine's Refresh: what moshi-cockpit there needs next (install, update, connect) or how many
// agents it has; nothing for another OS. The state text is its tooltip. Update only for an installed one the CLI
// found older than this Mac's (a newer one shows ✓ as usual); an unreadable version is the error state, "moshi: ?".
export function cockpitLink(c) {
  if (!c || typeof c !== "object" || c.state === "unsupported") return null;
  const count = Array.isArray(c.agents) ? c.agents.length : 0;
  const update = !!c.update && (c.state === "connected" || c.state === "unconnected");
  const text = c.state === "missing" ? "moshi: Install" : update ? "moshi: Update" : c.state === "unconnected"
    ? "moshi: Connect" : c.state === "connected" ? `moshi ✓ ${count}` : "moshi: ?";
  const install = c.state === "missing" || update ? argvOf(c.install_argv) : null;
  const connect = c.state === "unconnected" ? argvOf(c.connect_argv) : null;
  const action = install ? { name: c.state === "missing" ? "Install moshi-cockpit" : "Update moshi-cockpit", argv: install }
    : connect ? { name: "Connect to moshi", argv: connect } : null;
  return { text, tooltip: plain(c.state_text), action };
}

// New creates an agent with its own moshi identity there only with a connected, current moshi-cockpit.
export const moshiReady = (c) => c?.state === "connected" && !c.update;

// The harness chips of New's moshi form, from `machine`'s `agent list --json` offer rows. Each note names its harness.
export function moshiChips(harnesses, machine) {
  return (Array.isArray(harnesses) ? harnesses : []).filter((h) => Object.prototype.hasOwnProperty.call(NAMES, h?.name))
    .map((h) => ({
      name: h.name, label: NAMES[h.name], selectable: !!h.installed && !!h.available,
      note: h.installed ? (h.available ? "" : `${NAMES[h.name]}: ${plain(h.reason, 160)}`)
        : `${NAMES[h.name]} is not installed on ${plain(machine, 254) || "that machine"}`,
    }));
}

// Each typed value is one argument of its own; no shell is involved on the Mac, and the CLI validates them again.
// "--role=…" keeps a value that starts with "-" a value.
export const remoteAddArgs = (machine, form) => ["machine", "agent", "--on", machine, "add", form.name, "--harness", form.harness,
                                                 `--role=${form.role}`, `--skill=${form.skill}`, "--json"];
export const remoteLoadArgs = (machine, name) => ["machine", "agent", "--on", machine, "load", name, "--json"];
export const remoteCatalogArgs = (machine) => ["machine", "agent", "--on", machine, "catalog", "--json"];

export const REMOTE_APPROVAL = "The approval on this machine is missing or expired: Connect to moshi there";

// After a remote add or load: start the agent in a tab there, connect there (exit 3), or stay with the problem.
// Only the CLI's own answer counts: a start needs exit 0 and the command it names.
export function remoteSettle(r) {
  let answer = {};
  try {
    const parsed = JSON.parse(r.stdout);
    if (parsed && typeof parsed === "object") answer = parsed;
  } catch { /* not the CLI's answer: stderr says why */ }
  const runArgv = argvOf(answer.run_argv);
  if (r.code === 0 && runArgv) return { action: "start", runArgv };
  if (r.code === 3) return { action: "connect", problem: REMOTE_APPROVAL };
  const message = typeof answer.message === "string" ? answer.message : "";
  return { action: "stay", problem: plain(message || r.stderr, 300) || `moshi-cockpit failed (exit ${r.code})` };
}
