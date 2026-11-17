// "+ Agent": what the dialog makes of `agent catalog --json` (the From moshi tab) and of the answer to
// `agent add` / `agent load`. Pure, so `node --test` can load it (tests/js/agents.test.mjs).

import { outcome, plain } from "./rules.js";

// moshi's agents that are not here yet: harness, role, and where one runs now.
export function catalogRows(rows) {
  return (rows ?? []).map((r) => {
    const harness = plain(r.harness, 40);
    const active = plain(r.active_installation, 64);
    const loadable = !!r.loadable;
    return {
      name: plain(r.name, 64), harness, role: plain(r.role, 64),
      where: active ? `Running on ${active}` : "",
      loadable,
      note: loadable ? (r.harness_installed ? "" : `${harness} is not installed here`) : plain(r.reason, 200),
    };
  });
}

// After `agent add` or `agent load`: done, the connect dialog (exit 3: the approval is missing or expired), or
// stay with the problem shown inline. Staying never touches what the person typed (`form` comes back as it was).
export function settle(form, r) {
  const found = outcome(r);
  if (found.kind === "ok") return { action: "done", problem: null, out: found.out, form };
  if (found.kind === "connect") return { action: "connect", problem: null, form };
  return { action: "stay", problem: found.kind === "error" ? found.message : `Running on ${found.label}`, form };
}
