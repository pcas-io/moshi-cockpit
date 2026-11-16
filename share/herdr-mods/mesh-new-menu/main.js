// The "+" menu of the cockpit layout: herdr's new-menu (Apache-2.0) with the harnesses, plus "New agent…" and
// "Manage agents…". A harness opens its session list, where its own agents are too. moshi-cockpit generates the
// entries of the own agents into programs.json with this prefix; this menu leaves them out.
import { View, div, svg, image } from "gpui-kit";
import { h_flex, v_flex } from "gpui-base";
import { theme, spaces, panes, knownAgents, run, openOverlay, closeOverlay, props, window, keys, shared, setShared, home } from "herdr";

const KNOWN = "new-menu.agents";
const OWN = "moshi-cockpit-";
// herdr's standard entries whose sessions can be listed and resumed (moshi-cockpit sessions --harness).
const RESUMABLE = { claude: "claude", codex: "codex", grok: "grok", vibe: "vibe", "mistral-vibe": "vibe",
                    agy: "agy", antigravity: "agy" };

function shortcut(name) {
  const k = keys().find(b => b.section === "global" && b.action === name);
  if (!k?.keys.trim()) return "";
  const mac = window()?.platform === "macos";
  const names = mac ? { ctrl: "⌃", alt: "⌥", shift: "⇧", cmd: "⌘", fn: "fn" } : { ctrl: "Ctrl", alt: "Alt", shift: "Shift", cmd: "Super", fn: "Fn" };
  const alias = { secondary: mac ? "cmd" : "ctrl", super: "cmd", win: "cmd" };
  return k.keys.trim().split(/\s+/).map(stroke => {
    const parts = stroke.split("-");
    let key = parts.pop();
    if (key === "") { key = "-"; parts.pop(); }
    const held = new Set(parts.map(m => alias[m.toLowerCase()] ?? m.toLowerCase()));
    if (/^[A-Z]$/.test(key)) held.add("shift");
    const label = key.length === 1 ? key.toUpperCase() : key[0].toUpperCase() + key.slice(1).toLowerCase();
    return [...Object.keys(names).filter(m => held.has(m)).map(m => names[m]), label].join(mac ? "" : "+");
  }).join(" ");
}

export default class MeshNewMenu extends View {
  init(_props, cx) {
    const machine = spaces().find(s => s.active)?.machine;
    const where = machine ?? "";
    this.agents = shared(KNOWN)?.[where] ?? [];
    this.handle = cx.focus_handle();
    this.handle.focus();
    cx.spawn(async cx => {
      try {
        this.agents = await knownAgents({ on: machine });
        setShared(KNOWN, { ...shared(KNOWN), [where]: this.agents });
      }
      catch { this.failed = true; }
      cx.notify();
    });
  }
  row(id, icon, name, action, hint = "", enabled = true) {
    const t = theme();
    return h_flex().id(id).h(31).px(9).gap(10).rounded(7).items_center().flex_none()
      .text_color(enabled ? t.foreground : t.dim).when(enabled, el => el.hover(s => s.bg(t.selected)))
      .on_click(() => { if (enabled) { closeOverlay(props().overlay); action(); } })
      .child(icon).child(div().flex_1().child(name)).child(div().text_size(11).text_color(t.dim).child(hint));
  }
  heading(label) {
    return div().px(9).pt(8).pb(4).text_size(11).text_color(theme().dim).child(label);
  }
  divider() {
    return div().h(1).mx(4).my(5).bg(theme().border).flex_none();
  }
  agentIcon(a) {
    if (!a.icon) return div().w(14);
    return a.tint ? svg(a.icon).size(14).text_color(theme().foreground) : image(a.icon).size(14);
  }
  // A harness opens its session list first (mesh-agents, view "start"). Directly only what has no list: a program
  // we cannot read sessions for, or an agent on another machine (the list is local).
  start(a, space) {
    const harness = RESUMABLE[a.key];
    if (!harness || space?.machine) return run("tab.new", { name: a.name, space: space?.id, argv: a.run });
    openOverlay({ mod: "mesh-agents", kind: "dialog", width: 460, height: 460,
                  props: { view: "start", harness, run: a.run, label: a.name, space: space?.id,
                           folder: panes().find(p => p.focused)?.cwd } });
  }
  agentRow(a, space, number) {
    return this.row(a.key, this.agentIcon(a), a.name, () => this.start(a, space),
      a.installed ? (number <= 9 ? String(number) : "") : "not installed", a.installed);
  }
  manage(view) {
    openOverlay({ mod: "mesh-agents", kind: "dialog", width: 640, height: 460, props: { view } });
  }
  render() {
    const t = theme();
    const space = spaces().find(s => s.active);
    const standard = this.agents.filter(a => !a.key.startsWith(OWN));
    const installed = standard.filter(a => a.installed);
    const choices = [...installed, ...standard.filter(a => !a.installed).slice(0, 2)];
    const numbered = installed;
    const rows = Math.max(1, choices.length);
    return v_flex().min_h(0).w_full().h(Math.min(620, 260 + rows * 31)).bg(t.popover).p(5).text_size(12.8)
      .id("new-menu").track_focus(this.handle)
      .on_key_down((e) => {
        if (e.modifiers.control || e.modifiers.platform || e.modifiers.alt) return;
        const agent = /^[1-9]$/.test(e.key) ? numbered[Number(e.key) - 1] : null;
        if (!agent) return;
        closeOverlay(props().overlay);
        this.start(agent, space);
      })
      .child(this.row("terminal", svg("icons/shell.svg").size(14), "Terminal", () => run("tab.new", { space: space?.id }), shortcut("tab.new")))
      .child(v_flex().flex_1().min_h(0).overflow_y_scrollbar()
        .child(this.heading("Start a standard agent"))
        .children(choices.map((a, i) => this.agentRow(a, space, i + 1)))
        .when(this.agents.length === 0, el => el.child(div().px(9).py(6).text_color(t.dim).child(this.failed ? "Could not load agents" : "Checking installed agents…"))))
      .child(this.divider())
      .when(!!shared("moshi-cockpit.needs-connect"), el => el.child(this.row("connect", div().w(14).child("→"), "Mit moshi verbinden…",
        () => run("tab.new", { name: "Mit moshi verbinden", space: space?.id, argv: [`${home()}/.local/bin/moshi-cockpit`, "connect"] }))))
      .child(this.row("agent-new", div().w(14).child("+"), "New agent…", () => this.manage("new")))
      .child(this.row("agent-manage", div().w(14).child("≡"), "Manage agents…", () => this.manage("manage")))
      .child(this.divider())
      .child(this.row("space", div().w(14).child("+"), "New space", () => run("space.new"), shortcut("space.new")))
      .child(this.row("worktree", svg("mods/workspace-chrome/branch.svg").size(14), "New worktree…", () => openOverlay({ mod: "worktree", kind: "dialog", width: 460, height: 280, props: { space: space?.id, folder: panes().find(p => p.focused)?.cwd, action: "new" } }), "", Boolean(space)))
      .child(this.row("worktree-open", svg("mods/workspace-chrome/branch.svg").size(14), "Open worktree\u2026", () => openOverlay({ mod: "worktree", kind: "dialog", width: 560, height: 480, props: { space: space?.id, folder: panes().find(p => p.focused)?.cwd, action: "open" } }), "", Boolean(space)));
  }
}
