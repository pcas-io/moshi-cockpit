// Pure checks for the panel's "+ Agent" and "Entfernen" (no host imports, so `node --test` can load
// this file: tests/js/rules.test.mjs keeps it in step with cockpit/registry.py). The CLI checks
// everything again; these only keep obvious mistakes from reaching it.

// moshi's agent name rule (fullmatch) and the names moshi-cockpit reserves (registry.NAME_RE, RESERVED).
export const NAME_RE = /^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$/;
export const RESERVED = new Set(["admin", "broadcast", "cockpit", "register"]);
export const MAX_ROLE = 120;

export function checkName(raw, local) {
  const name = String(raw ?? "");
  if (!NAME_RE.test(name)) return "Name: Buchstaben, Ziffern, _ und -, am Anfang Buchstabe oder Ziffer";
  if (RESERVED.has(name.toLowerCase())) return `'${name}' ist reserviert`;
  if ((local ?? []).some((a) => a.name.toLowerCase() === name.toLowerCase())) return `Es gibt schon '${name}'`;
  return null;
}

// Role: one line, control characters out, bounded, like registry._validate.
export const roleText = (raw) => String(raw ?? "").replace(/[\u0000-\u001f\u007f-\u009f]+/g, " ")
  .split(/\s+/).filter(Boolean).join(" ")
  // By code point like Python's [:120]: an emoji at the edge is never cut in half.
  .match(/[\s\S]/gu)?.slice(0, MAX_ROLE).join("") ?? "";

// An ocellio skill name: optional, the same characters as an agent name (registry._validate).
export const checkSkill = (raw) => (!raw || NAME_RE.test(String(raw)) ? null : "Skill: letters, digits, _ and - only");

// Values in --x= form: nothing a person types can be read as an option. No shell is involved.
export const addArgs = (name, harness, role, connect = false, skill = "") =>
  ["agent", "add", name, `--harness=${harness}`, ...(role ? [`--role=${role}`] : []), ...(skill ? [`--skill=${skill}`] : []),
   ...(connect ? ["--connect"] : [])];

export const setArgs = (name, role, skill) => {
  if (!NAME_RE.test(String(name ?? ""))) throw new Error("ungültiger Agent-Name");
  return ["agent", "set", name, `--role=${role}`, `--skill=${skill}`];
};

// Only a name that passes the rule is ever sent (it cannot start with "-"). The panel asks first, so the
// moshi decision is always explicit: revoke (admin access there) or keep.
export const removeArgs = (name, revoke = false) => {
  if (!NAME_RE.test(String(name ?? ""))) throw new Error("ungültiger Agent-Name");
  return ["agent", "remove", name, revoke ? "--revoke-moshi" : "--keep-moshi"];
};
