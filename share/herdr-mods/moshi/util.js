// Small helpers of the "Your agents" list.

import { div } from "gpui-kit";

// One button style for the whole panel, sized for easy clicking. on = selected, enabled = false for a
// disabled one, primary = the one main action (green, the theme's "done" color in light and dark).
export function btn(id, t, label, onClick, { on = false, enabled = true, primary = false } = {}) {
  const el = div().id(id).px(10).py(4).rounded(6).border_1().text_size(13)
    .border_color(primary ? t.done : on ? t.accent : t.border).bg(primary ? t.done : on ? t.raised : t.surface)
    .text_color(primary ? t.background : enabled ? t.foreground : t.dim)
    .when(primary, (e) => e.font_semibold());
  if (!enabled) return el.child(label);
  return el.hover((s) => (primary ? s.border_color(t.foreground) : s.bg(t.hover))).on_click(onClick).child(label);
}

export function ago(iso) {
  const t = Date.parse(iso || "");
  if (!t) return "–";
  const min = Math.max(0, Math.round((Date.now() - t) / 60000));
  if (min < 1) return "now";
  if (min < 60) return `${min} min`;
  if (min < 48 * 60) return `${Math.round(min / 60)} h`;
  return `${Math.round(min / 1440)} d`;
}

export function dotColor(t, presence) {
  if (presence === "live") return t.working;
  if (presence === "stale") return t.idle;
  return t.dim;
}

// Control characters (escapes, BEL, CR/LF) out of any text that came from a server or a process.
export function plain(text, max = 300) {
  return String(text ?? "").replace(/[\u0000-\u001f\u007f-\u009f]+/g, " ").trim().slice(0, max);
}

// Runs a moshi-cockpit subcommand and returns parsed JSON (or the raw text with asJson=false).
export async function cockpit(exec, home, args, asJson = true) {
  const r = await exec(`${home()}/.local/bin/moshi-cockpit`, args);
  if (r.code !== 0) throw new Error(plain(r.stderr || `moshi-cockpit ${args[0]} failed`).replace(/^Error: /, ""));
  return asJson ? JSON.parse(r.stdout) : r.stdout.trim();
}
