// The harness list of "+ Agent" → New: our five harnesses (from `moshi-cockpit agent list --json`) joined with
// herdr's knownAgents(). Pure, so `node --test` can load it (tests/js/agents.test.mjs).

// Our harness → herdr's program key, as knownAgents() names them (checked against a real herdr-app 0.1.16:
// claude, codex, vibe, grok, and agy is "antigravity").
export const HERDR_APP = { claude: "claude", codex: "codex", vibe: "vibe", agy: "antigravity", grok: "grok" };

// herdr's display names, for when knownAgents() has not answered.
export const NAMES = { claude: "Claude Code", codex: "Codex", vibe: "Vibe", agy: "Antigravity", grok: "Grok" };

// Shown when herdr finds a harness (its login shell may define it) that `moshi-cockpit run` cannot start.
export const HERDR_ONLY = "found by Herdr, not on moshi-cockpit's PATH";

// offer: the CLI's `harnesses` rows {name, installed, available, reason}; known: knownAgents() (may be missing
// when its check failed); cli: the path of moshi-cockpit. Selectable only when the CLI finds it, since `run`
// starts it from there. herdr finding it alone gives a note and the install action; missing everywhere → an
// install tab with the vendor's official installer.
export function harnessRows(offer, known, cli) {
  const byKey = new Map((known ?? []).map((a) => [a.key, a]));
  return (offer ?? []).filter((h) => h.name in HERDR_APP).map((h) => {
    const app = byKey.get(HERDR_APP[h.name]);
    const label = app?.name || NAMES[h.name];
    const installed = !!h.installed;
    const herdrOnly = !installed && !!app?.installed;
    return {
      name: h.name, label, installed, herdrOnly,
      selectable: installed && !!h.available,
      note: installed ? (h.available ? "" : String(h.reason ?? "")) : herdrOnly ? HERDR_ONLY : "not installed",
      install: installed ? null : { name: `Install ${label}`, argv: [cli, "harness", "install", h.name] },
    };
  });
}
