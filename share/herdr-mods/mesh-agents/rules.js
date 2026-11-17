// Pure checks and CLI arguments for the panel's "+ Agent", "Remove" and starts (no host imports, so `node --test`
// can load this file: tests/js/rules.test.mjs keeps it in step with cockpit/registry.py). The CLI checks
// everything again; these only keep obvious mistakes from reaching it.

// moshi's agent name rule (fullmatch) and the names moshi-cockpit reserves (registry.NAME_RE, RESERVED).
export const NAME_RE = /^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$/;
export const RESERVED = new Set(["admin", "broadcast", "cockpit", "default", "device", "register"]);
export const MAX_ROLE = 64;  // moshi's FIELD_LIMITS.ROLE, like registry.MAX_ROLE

export function checkName(raw, local) {
  const name = String(raw ?? "");
  if (!NAME_RE.test(name)) return "Name: letters, digits, _ and -, starting with a letter or digit";
  if (RESERVED.has(name.toLowerCase())) return `'${name}' is reserved`;
  if ((local ?? []).some((a) => a.name.toLowerCase() === name.toLowerCase())) return `'${name}' exists already`;
  return null;
}

// Role: one line, control characters out, bounded, like registry._validate.
export const roleText = (raw) => boundRole(String(raw ?? "").replace(/[\u0000-\u001f\u007f-\u009f]+/g, " ")
  .split(/\s+/).filter(Boolean).join(" "));

// At most MAX_ROLE UTF-16 units, as moshi counts (`.length`), never cutting a character in half
// (registry.bound_role).
const boundRole = (text) => {
  let out = "";
  for (const char of text) {
    if (out.length + char.length > MAX_ROLE) break;
    out += char;
  }
  return out;
};

// An ocellio skill name: optional, the same characters as an agent name (registry._validate).
export const checkSkill = (raw) => (!raw || NAME_RE.test(String(raw)) ? null : "Skill: letters, digits, _ and - only");

// Values in --x= form: nothing a person types can be read as an option. No shell is involved.
export const addArgs = (name, harness, role, skill = "") =>
  ["agent", "add", name, `--harness=${harness}`, ...(role ? [`--role=${role}`] : []), ...(skill ? [`--skill=${skill}`] : [])];

// Only a name that passes the rule is ever sent as an argument of its own (it cannot start with "-").
const named = (name) => {
  if (!NAME_RE.test(String(name ?? ""))) throw new Error("invalid agent name");
  return name;
};

export const setArgs = (name, role, skill) => ["agent", "set", named(name), `--role=${role}`, `--skill=${skill}`];

// Ends this installation's token in moshi and removes the agent here; moshi keeps the agent.
export const removeArgs = (name) => ["agent", "remove", named(name)];

export const loadArgs = (name) => ["agent", "load", named(name)];

export const whereArgs = (name) => ["agent", "where", named(name), "--json"];

// `--take-over` right after the name is the CLI's own flag; everything after it goes to the harness.
export const runArgv = (cli, name, takeOver = false, extra = []) =>
  [cli, "run", named(name), ...(takeOver ? ["--take-over"] : []), ...extra];

// Control characters (escapes, BEL, CR/LF) out of any text that came from a server or a process.
export const plain = (text, max = 300) => String(text ?? "").replace(/[\u0000-\u001f\u007f-\u009f]+/g, " ").trim().slice(0, max);

// What a finished `moshi-cockpit` call means for the panel. Exit codes are shared with the CLI: 3 = approval
// required (connect again), 4 = the agent is active on another installation (stdout `{"active": label}`).
export function outcome(r) {
  if (r.code === 0) return { kind: "ok", out: String(r.stdout ?? "").trim() };
  if (r.code === 3) return { kind: "connect" };
  if (r.code === 4) {
    try {
      const label = JSON.parse(r.stdout).active;
      if (typeof label === "string" && label) return { kind: "active", label: plain(label, 64) };
    } catch { /* not the CLI's answer: an error like any other */ }
  }
  const message = plain(r.stderr, 300).replace(/^Error: /, "");
  return { kind: "error", message: message || `moshi-cockpit failed (exit ${r.code})` };
}

// Before an own agent starts: the answer of `agent where --json`. Held by another installation → ask first;
// not connected → the connect dialog; anything else (no moshi, moshi not reachable) → start, `run` decides then.
export function takeoverFrom(r) {
  if (r.code === 3) return { kind: "connect" };
  if (r.code !== 0) return { kind: "start" };
  let found;
  try {
    found = JSON.parse(r.stdout);
  } catch {
    return { kind: "start" };
  }
  const label = typeof found?.active === "string" ? plain(found.active, 64) : "";
  if (!label || found.here) return { kind: "start" };
  return { kind: "ask", label, question: `Running on ${label}. Take over here?` };
}
