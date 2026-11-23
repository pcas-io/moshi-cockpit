// "Your agents" in the sidebar, below herdr's Spaces and Agents: the person's own agents
// with their moshi presence. Reads only `moshi-cockpit status --json`: the token stays in the keychain behind the
// CLI, this mod never sees it. Starting, creating and removing live in the mesh-agents dialog. The agents of Linux
// machines ("on <machine>") come in the same answer, from the CLI's cache of those machines: no hc call, no new timer.

import { View, div } from "gpui-kit";
import { v_flex, h_flex } from "gpui-base";
import { agents, exec, home, layout, notify, openOverlay, panes, run, setShared, spaces, theme } from "herdr";
import { ago, btn, cockpit, dotColor, plain } from "./util.js";
import { ownAgents, remoteCommand, runningTerminal, sidebarLine } from "./list.js";

const POLL_MS = 30000;
const CONNECT_POLL_MS = 3000; // back in herdr right after the connect tab finished, not half a minute later
const NEEDS_CONNECT = "moshi-cockpit.needs-connect"; // the "+" menu shows its fallback entry while this is true

function find(node, id) {
  if (node?.id === id) return node;
  for (const child of node?.children ?? []) {
    const found = find(child, id);
    if (found) return found;
  }
  return null;
}

// The same test as herdr's own Agents list: the rail goes by the sidebar's width.
const narrow = () => (find(layout(), "sidebar")?.size?.px ?? Infinity) < 160;

export default class Moshi extends View {
  init(_props, cx) {
    this.data = null;
    this.error = null;
    this.loading = false;
    this.asked = false;
    this.finding = null;
    this.checking = false;
    cx.timer.every(POLL_MS, (cx) => this.refresh(cx));
    cx.timer.every(CONNECT_POLL_MS, (cx) => { if (this.data?.needs_connect) this.refresh(cx); });
    // Once at the start; again only while something is wrong, so that the line goes away once it is fixed.
    cx.timer.every(POLL_MS, (cx) => { if (this.finding) this.checkHealth(cx); });
    this.checkHealth(cx);
    this.refresh(cx);
  }

  // Silent while all is well; a failed check of its own is no finding.
  checkHealth(cx) {
    if (this.checking) return;
    this.checking = true;
    cx.spawn(async (cx) => {
      try {
        const r = await exec(`${home()}/.local/bin/moshi-cockpit`, ["doctor", "--quiet"]);
        this.finding = r.code !== 0 && r.stdout.trim() ? plain(r.stdout, 400) : null;
      } catch {
        this.finding = null;
      } finally {
        this.checking = false;
      }
      cx.notify();
    });
  }

  // Once per start of herdr: closed without connecting, the "+" menu still has the way.
  // Disconnected or unreachable never open it: the sidebar's one line says so, and a click there opens it.
  offerConnect() {
    setShared(NEEDS_CONNECT, !!this.data?.needs_connect);
    if (!this.data?.needs_connect || this.asked) return;
    this.asked = true;
    this.openConnect();
  }

  openConnect() {
    openOverlay({ mod: "mesh-agents", kind: "dialog", width: 420, height: 220, props: { view: "connect" } });
  }

  newAgent() {
    openOverlay({ mod: "mesh-agents", kind: "dialog", width: 640, height: 460, props: { view: "new" } });
  }

  refresh(cx) {
    if (this.loading) return;
    this.loading = true;
    cx.spawn(async (cx) => {
      try {
        this.data = await cockpit(exec, home, ["status", "--json"]);
        this.error = null;
        this.offerConnect();
      } catch (e) {
        this.error = plain(e?.message ?? e, 200);
      } finally {
        this.loading = false;
      }
      cx.notify();
    });
  }

  // A local agent: always the choice of the "+" menu, new or resume; a running tab is one more entry there.
  // An agent of a Linux machine: its running tab, else `moshi-cockpit run` in a tab there (which asks to take over).
  activate(a) {
    const running = runningTerminal(a.name, agents());
    if ((a.machine || !a.installed) && running) return run("agent.focus", { terminal: running });
    if (a.machine) {
      const start = remoteCommand(spaces(), a);
      return start ? run(start.command, start.args) : undefined;
    }
    if (!a.installed) {
      return notify({ title: "Your agents", body: `${a.name}: ${a.harness} is not installed here`, level: "info" });
    }
    openOverlay({ mod: "mesh-agents", kind: "dialog", width: 460, height: 460,
                  props: { view: "start", agent: a.name, label: a.name, space: spaces().find((s) => s.active)?.id,
                           folder: panes().find((p) => p.focused)?.cwd,
                           terminal: running ?? undefined } });
  }

  // One line, sizes as in herdr's Agents list: name and status in the list's text size, the time smaller.
  row(t, a) {
    const terminal = runningTerminal(a.name, agents());
    const about = [a.harness, a.role, a.installed ? "" : "not installed", a.machine ? `on ${a.machine}` : ""]
      .filter(Boolean).join(" · ");
    const when = [terminal ? "↗" : "", a.last_seen_at ? ago(a.last_seen_at) : ""].filter(Boolean).join(" · ");
    return h_flex().id(`own-${a.name}`).px(8).h(30).flex_none().rounded(8).items_center().gap(9).cursor_pointer()
      .hover((s) => s.bg(t.hover)).tooltip(about).on_click(() => this.activate(a))
      .child(div().flex_none().w(8).h(8).rounded(4).bg(dotColor(t, a.presence)))
      .child(h_flex().flex_1().min_w(0).gap(5).items_center()
        .child(div().min_w(0).max_w(a.working_on ? 120 : 200).truncate().text_color(a.installed ? t.foreground : t.dim).child(a.name))
        .when(!!a.working_on, (el) => el.child(div().flex_1().min_w(0).truncate().text_color(t.dim).child(`· ${plain(a.working_on)}`))))
      .when(!!when, (el) => el.child(div().flex_none().text_size(11).text_color(t.dim).child(when)));
  }

  // Dots only in the rail, as herdr's list does there.
  dot(t, a) {
    return h_flex().id(`own-${a.name}`).h(26).flex_none().items_center().justify_center()
      .hover((s) => s.bg(t.hover)).tooltip(a.machine ? `${a.name} on ${a.machine}` : a.name).on_click(() => this.activate(a))
      .child(div().w(8).h(8).rounded(4).bg(dotColor(t, a.presence)));
  }

  render() {
    const t = theme();
    const list = ownAgents(this.data?.local, this.data?.agents, this.data?.remote);
    const collapsed = narrow();
    const line = sidebarLine(this.data);
    return v_flex().id("own-agents").size_full().min_h(0).bg(t.chrome).border_t(1).border_color(t.border).text_size(13)
      .when(!collapsed, (el) => el.child(
        h_flex().px(16).h(41).pt(14).pb(5).flex_none().items_center().justify_between()
          .child(div().text_size(11.5).font_medium().text_color(t.dim).child("Your agents"))
          .child(div().id("own-new").text_size(13).text_color(t.dim).hover((s) => s.text_color(t.foreground))
            .tooltip("+ Agent")
            .on_click(() => this.newAgent())
            .child("+"))))
      // One line about moshi at most: the connection line wins over the health check, which wins over an error.
      .when(!collapsed && !!line, (el) => el.child(
        div().id("own-connection").px(16).pb(4).text_size(12).text_color(line.connect ? t.blocked : t.dim)
          .when(line.connect, (d) => d.cursor_pointer().hover((s) => s.text_color(t.foreground)).on_click(() => this.openConnect()))
          .child(line.text)))
      .when(!collapsed && !line && !!this.error && !this.finding, (el) => el.child(
        div().px(16).pb(4).text_size(12).text_color(t.blocked).child(this.error)))
      .when(!collapsed && !line && !!this.finding, (el) => el.child(
        div().px(16).pb(4).text_size(12).text_color(t.blocked).child(this.finding)))
      // A list that breaks a one-agent rule (agy): shown, starts refused by the CLI, way out named.
      .when(!collapsed, (el) => el.children((this.data?.problems ?? []).map((p) =>
        div().px(16).pb(4).text_size(12).text_color(t.blocked).child(plain(p)))))
      .when(!collapsed && !!this.data && !this.data.needs_connect && !list.length, (el) => el.child(
        h_flex().px(16).pt(2).child(btn("own-first", t, "+ Agent", () => this.newAgent()))))
      .child(v_flex().id("own-list").flex_1().min_h(0).px(collapsed ? 0 : 8).overflow_y_scrollbar()
        .children(list.map((a) => (collapsed ? this.dot(t, a) : this.row(t, a)))));
  }
}
