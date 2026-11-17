// Clicking an agent in the "+" menu or under "Your agents": a new session, or one to resume, in the style of
// "Manage agents…". Sessions come from `moshi-cockpit sessions`; their titles are the person's own text, cleaned
// there. With none to resume the agent just starts. An own agent starts through `moshi-cockpit run`, with its
// identity; a standard agent with herdr's own command, as the person set it up (`panel.run`).
// Every start of an own agent asks `agent where --json` first: when another installation holds it, the dialog
// asks "Running on {label}. Take over here?" and only a yes starts it with --take-over.

import { div } from "gpui-kit";
import { v_flex, h_flex } from "gpui-base";
import { exec, home, run, props, closeOverlay } from "herdr";
import { ago, btn, cockpit, plain } from "./util.js";
import { runArgv, takeoverFrom, whereArgs } from "./rules.js";
import { openConnect } from "./connect.js";

const LIMIT = 8;
const cli = () => `${home()}/.local/bin/moshi-cockpit`;
const shortFolder = (path) => (path || "").replace(home(), "~").split("/").slice(-2).join("/");
// grok's own config can approve everything without asking: from here it always asks.
const ASKING = { grok: ["--permission-mode", "default"] };

function close() {
  closeOverlay(props().overlay);
}

// Opens the tab. Once per dialog: Enter or a click during loading must not race the automatic start of an empty
// list, and a second click while `where` is asked must not start a second tab.
function open(panel, tab, takeOver = false) {
  if (panel.started) return;
  panel.started = true;
  const argv = tab.agent ? runArgv(cli(), tab.agent, takeOver, tab.extra)
    : [...panel.run, ...(ASKING[panel.harness] ?? []), ...tab.extra];
  run("tab.new", { name: tab.name, space: panel.space, folder: tab.folder, argv });
  close();
}

// A standard agent opens at once; an own agent only after `where` said that no other installation holds it.
function begin(panel, cx, tab) {
  if (panel.started || panel.checking || panel.ask) return;
  if (!tab.agent) return open(panel, tab);
  panel.checking = true;
  cx.notify();
  cx.spawn(async (cx) => {
    let r;
    try {
      r = await exec(cli(), whereArgs(tab.agent));
    } catch {
      r = { code: 1, stdout: "", stderr: "" };  // could not ask: `run` decides, as from a terminal
    }
    panel.checking = false;
    const next = takeoverFrom(r);
    if (next.kind === "connect") return openConnect(props().overlay);
    if (next.kind === "start") return open(panel, tab);
    panel.ask = { question: next.question, tab };
    cx.notify();
  });
}

export function startNew(panel, cx) {
  begin(panel, cx, { name: panel.label, agent: panel.run ? null : panel.agent, extra: [] });
}

// A harness's own agent: a new session at once, with its identity, skill and role.
export function startAgent(panel, cx, agent) {
  begin(panel, cx, { name: agent.name, agent: agent.name, extra: [] });
}

export function resume(panel, cx, session) {
  begin(panel, cx, { name: panel.label, agent: panel.run ? null : panel.agent, folder: session.cwd || undefined,
                     extra: session.resume_args ?? [] });
}

// `here` and `all` never list a session twice: what is in this folder is not repeated below it.
export async function loadSessions(panel, cx) {
  const who = panel.harness ? [`--harness=${panel.harness}`] : [panel.agent];
  const read = (extra) => cockpit(exec, home, ["sessions", ...who, "--json", "--limit", String(LIMIT), ...extra]);
  try {
    const here = panel.folder ? (await read([`--folder=${panel.folder}`])).sessions ?? [] : [];
    const ids = new Set(here.map((s) => s.id));
    const all = ((await read([])).sessions ?? []).filter((s) => !ids.has(s.id));
    const agents = panel.harness ? ((await cockpit(exec, home, ["agent", "list", "--json"])).agents ?? [])
      .filter((a) => a.harness === panel.harness && a.installed) : [];
    panel.agents = agents;
    panel.here = here;
    panel.all = all;
    panel.loaded = true;
    panel.at = -1;
    if (agents.length + here.length + all.length === 0) return startNew(panel, cx);  // nothing to choose from
  } catch (e) {
    panel.error = plain(e?.message ?? e, 200);
    panel.loaded = true;
  }
  cx.notify();
}

const owned = (panel) => panel.agents ?? [];
const choices = (panel) => [...owned(panel), ...panel.here, ...panel.all];

export function startKey(panel, e, cx) {
  if (panel.ask) return;  // taking over is a click, never a leftover Enter
  const list = choices(panel);
  if (e.key === "down") panel.at = Math.min(panel.at + 1, list.length - 1);
  else if (e.key === "up") panel.at = Math.max(panel.at - 1, -1);
  else if (e.key === "enter") {
    if (panel.at < 0) return startNew(panel, cx);
    return panel.at < owned(panel).length ? startAgent(panel, cx, list[panel.at]) : resume(panel, cx, list[panel.at]);
  }
  else return;
  cx.notify();
}

function sessionRow(panel, t, s, index, withFolder) {
  const on = panel.at === index;
  return h_flex().id(`session-${s.id}`).w_full().min_w_0().h(30).px(9).gap(10).rounded(7).items_center().flex_none()
    .bg(on ? t.selected : t.popover).hover((st) => st.bg(t.selected))
    .on_click((_e, cx) => resume(panel, cx, s))
    .child(div().flex_1().min_w_0().text_size(13).text_color(t.foreground).truncate().child(s.title))
    .child(div().flex_shrink_0().text_size(11).text_color(t.dim).child(
      withFolder ? `${shortFolder(s.cwd)} · ${ago(s.updated)}` : ago(s.updated)));
}

function agentRow(panel, t, a, index) {
  const on = panel.at === index;
  return h_flex().id(`start-agent-${a.name}`).w_full().min_w_0().h(30).px(9).gap(10).rounded(7).items_center().flex_none()
    .bg(on ? t.selected : t.popover).hover((st) => st.bg(t.selected))
    .on_click((_e, cx) => startAgent(panel, cx, a))
    .child(div().flex_shrink_0().text_size(13).text_color(t.foreground).child(a.name))
    .child(div().flex_1().min_w_0().text_size(11).text_color(t.dim).truncate().child(
      [a.role, a.skill].filter(Boolean).join(" · ")));
}

function heading(t, label) {
  return div().px(9).pt(8).pb(4).text_size(11).text_color(t.dim).flex_none().child(label);
}

// The takeover question in place of the list: Take over starts here with --take-over, Cancel starts nothing.
function askView(panel, t) {
  return v_flex().size_full().p(24).gap(12).bg(t.popover).items_center().justify_center()
    .child(div().font_semibold().text_size(16).text_color(t.foreground).child(panel.ask.tab.name))
    .child(div().text_size(13).text_color(t.foreground).text_center().child(panel.ask.question))
    .child(h_flex().gap(8).pt(6)
      .child(btn("takeover-yes", t, "Take over", () => open(panel, panel.ask.tab, true), { primary: true }))
      .child(btn("takeover-no", t, "Cancel", () => close())));
}

export function startView(panel, t) {
  if (panel.ask) return askView(panel, t);
  const newOn = panel.at < 0;
  const agents = owned(panel);
  const offset = agents.length + panel.here.length;
  return v_flex().size_full().p(8).gap(2).bg(t.popover).text_size(12.8)
    .child(div().px(9).pt(4).pb(8).font_semibold().text_size(16).text_color(t.foreground).flex_none().child(panel.label))
    .when(!!panel.terminal, (el) => el.child(h_flex().id("session-running").w_full().h(34).px(9).gap(10).rounded(7)
      .items_center().flex_none().hover((s) => s.bg(t.selected)).on_click(() => { run("agent.focus", { terminal: panel.terminal }); close(); })
      .child(div().flex_1().text_size(13).text_color(t.foreground).child("↗ Go to running tab"))))
    .child(h_flex().id("session-new").w_full().h(34).px(9).gap(10).rounded(7).items_center().flex_none()
      .bg(newOn ? t.selected : t.popover).hover((s) => s.bg(t.selected))
      .on_click((_e, cx) => startNew(panel, cx))
      .child(div().flex_1().font_semibold().text_size(13).text_color(t.foreground).child("▶ New session"))
      .child(div().text_size(11).text_color(t.dim).child("Enter")))
    .child(v_flex().flex_1().min_h(0).overflow_y_scrollbar()
      .when(!panel.loaded, (el) => el.child(div().px(9).py(8).text_color(t.dim).child("Loading sessions…")))
      .when(agents.length > 0, (el) => el.child(heading(t, "Agents"))
        .children(agents.map((a, i) => agentRow(panel, t, a, i))))
      .when(panel.here.length > 0, (el) => el.child(heading(t, "This folder"))
        .children(panel.here.map((s, i) => sessionRow(panel, t, s, agents.length + i, false))))
      .when(panel.all.length > 0, (el) => el.child(heading(t, panel.here.length ? "All folders" : "Recent sessions"))
        .children(panel.all.map((s, i) => sessionRow(panel, t, s, offset + i, true)))))
    .when(!!panel.error, (el) => el.child(div().px(9).text_size(12).text_color(t.blocked).child(panel.error)))
    .when(!!panel.checking, (el) => el.child(div().px(9).text_size(12).text_color(t.dim).child("Checking moshi…")))
    .when(!panel.harness, (el) => el
      .child(div().h(1).mx(4).my(5).bg(t.border).flex_none())
      .child(h_flex().id("session-manage").w_full().h(30).px(9).rounded(7).items_center().flex_none()
        .hover((s) => s.bg(t.selected)).on_click(() => panel.manageAgent())
        .child(div().text_size(13).text_color(t.foreground).child("Manage agent…"))));
}
