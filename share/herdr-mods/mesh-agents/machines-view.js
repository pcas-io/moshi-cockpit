// "+ Agent" → Machines: this Mac and Herdr's machines, the six harnesses on each (`machine inventory`), and the
// ssh hosts not added yet (`machine add` in a visible tab). Opening the tab asks once; "Refresh" asks one machine
// again; nothing polls. Install and Sign in open a tab where the command runs (on that machine for a remote one).
// The logic lives in machines.js (tested with node); `panel` is the MeshAgents dialog, which keeps the state.

import { div } from "gpui-kit";
import { v_flex, h_flex } from "gpui-base";
import { exec, home, run, props, closeOverlay, spaces, machines } from "herdr";
import { ago, btn, cockpit, plain } from "./util.js";
import { INVENTORY_EXEC, STATE_TEXT, addHostCommand, connectingNote, harnessAction, hostRows, inventoryArgs, layoutReady, machineRows,
         tabCommand } from "./machines.js";

const cli = () => `${home()}/.local/bin/moshi-cockpit`;
const keyOf = (name) => name ?? "";

function inspect(panel, name, refresh, cx) {
  const key = keyOf(name);
  panel.inventory[key] = { ...panel.inventory[key], loading: true, error: null };
  cx.spawn(async (cx) => {
    let next;
    try {
      const data = await cockpit(exec, home, inventoryArgs(name, refresh), true, INVENTORY_EXEC);
      next = { loading: false, data: Array.isArray(data?.harnesses) ? data : null,
               error: Array.isArray(data?.harnesses) ? null : "moshi-cockpit gave no inventory" };
    } catch (e) {
      next = { loading: false, data: null, error: plain(e?.message ?? e, 300) };  // never a stale "Ready"
    }
    panel.inventory[key] = next;
    cx.notify();
  });
}

// This Mac is asked at once; Herdr's machines as soon as the list says which are connected, all in parallel.
export function loadMachines(panel, cx) {
  Object.assign(panel, { machines: machineRows(null), sshHosts: null, machinesProblem: null, inventory: {},
                         machineAsk: null });
  inspect(panel, null, false, cx);
  cx.spawn(async (cx) => {
    try {
      const list = await cockpit(exec, home, ["machine", "list", "--json"]);
      panel.machines = machineRows(list);
      panel.sshHosts = hostRows(list?.ssh_hosts);
      panel.machinesProblem = list?.problem ? plain(list.problem, 300) : null;
      for (const m of panel.machines) if (m.name && m.connected) inspect(panel, m.name, false, cx);
    } catch (e) {
      panel.sshHosts = false;  // unknown: no section rather than "No new hosts"
      panel.machinesProblem = plain(e?.message ?? e, 300);
    }
    cx.notify();
  });
}

export function refreshMachine(panel, name, cx) {
  if (panel.inventory?.[keyOf(name)]?.loading) return;
  panel.machineAsk = null;
  inspect(panel, name, true, cx);
  cx.notify();
}

function openTab(machine, label, action) {
  const name = action.kind === "install" ? `Install ${label}` : `Sign in to ${label}`;
  const next = tabCommand(spaces(), machine, name, action.argv);
  run(next.command, next.args);
  closeOverlay(props().overlay);
}

function act(panel, m, h, action, cx) {
  if (!action.confirm) return openTab(m.name, plain(h.label, 40), action);
  panel.machineAsk = { machine: m.name, harness: h.name };
  cx.notify();
}

function addHost(alias) {
  const next = addHostCommand(spaces(), cli(), alias);
  run(next.command, next.args);
  closeOverlay(props().overlay);
}

function note(t, text, color = t.dim) {
  return div().text_size(12).text_color(color).child(text);
}

function askRow(panel, t, m, h, action, id) {
  return v_flex().w_full().gap(6).px(8).py(6).rounded(6).bg(t.raised).flex_none()
    .child(note(t, plain(action.confirm, 400), t.foreground))
    .child(h_flex().gap(6)
      .child(btn(`${id}-yes`, t, "Install", () => openTab(m.name, plain(h.label, 40), action), { primary: true }))
      .child(btn(`${id}-no`, t, "Cancel", (_e, cx) => { panel.machineAsk = null; cx.notify(); })));
}

function harnessLine(panel, t, m, h, id, ready) {
  const action = harnessAction(h, m.name, ready);
  const asking = !!action.kind && panel.machineAsk?.machine === m.name && panel.machineAsk?.harness === h.name;
  return v_flex().w_full().gap(4).flex_none()
    .child(h_flex().w_full().min_w_0().gap(8).px(8).py(2).items_center()
      .child(div().w(120).flex_shrink_0().text_size(13).text_color(t.foreground).truncate().child(plain(h.label, 40)))
      .child(div().flex_1().min_w_0().text_size(12).text_color(h.state === "ready" ? t.foreground : t.dim).truncate()
        .child(STATE_TEXT[h.state] ?? plain(h.state_text, 60)))
      .when(!!action.note, (el) => el.child(div().flex_shrink_0().text_size(11).text_color(t.dim).child(action.note)))
      .when(!!action.kind, (el) => el.child(btn(id, t, action.label, (_e, cx) => act(panel, m, h, action, cx), { on: asking }))))
    .when(asking, (el) => el.child(askRow(panel, t, m, h, action, `${id}-ask`)));
}

function machineSection(panel, t, m, index) {
  const inv = m.connected ? panel.inventory?.[keyOf(m.name)] : null;  // never an unsupported name's key
  const id = `machine-${index}`;
  const status = !m.connected ? m.note : inv?.loading ? "Checking…" : inv?.data ? `checked ${ago(inv.data.checked_at)}` : "";
  const ready = layoutReady(machines(), m.name);  // Install and Sign in open a tab there: only once Herdr can
  return v_flex().id(id).w_full().gap(2).pb(6).flex_none()
    .child(h_flex().w_full().min_w_0().gap(8).px(8).py(4).items_center()
      .child(div().flex_shrink_0().font_semibold().text_size(13).text_color(m.connected ? t.foreground : t.dim).child(m.label))
      .child(div().flex_1().min_w_0().text_size(11).text_color(t.dim).truncate().child(status))
      .when(m.connected, (el) => el.child(btn(`${id}-refresh`, t, "Refresh", (_e, cx) => refreshMachine(panel, m.name, cx),
        { enabled: !inv?.loading }))))
    .when(!!inv?.error, (el) => el.child(div().px(8).child(note(t, inv.error, t.blocked))))
    .when(!!inv?.data && !ready, (el) => el.child(div().px(8).child(note(t, connectingNote(m.label)))))
    .children((inv?.data?.harnesses ?? []).map((h) => harnessLine(panel, t, m, h, `${id}-${plain(h.name, 20)}`, ready)));
}

function hostsSection(panel, t) {
  const hosts = panel.sshHosts;
  return v_flex().w_full().gap(2).pt(4).flex_none()
    .child(div().px(8).pb(2).text_size(11).text_color(t.dim).child("From ~/.ssh/config"))
    .when(hosts === null, (el) => el.child(div().px(8).child(note(t, "Checking…"))))
    .when(Array.isArray(hosts) && hosts.length === 0, (el) => el.child(div().px(8).child(note(t, "No new hosts in ~/.ssh/config."))))
    .children((hosts || []).map((h, i) => h_flex().w_full().min_w_0().gap(8).px(8).py(2).items_center().flex_none()
      .child(div().flex_1().min_w_0().text_size(13).text_color(h.ok ? t.foreground : t.dim).truncate().child(h.label))
      .when(!h.ok, (el) => el.child(div().flex_shrink_0().text_size(11).text_color(t.dim).child(h.note)))
      .child(btn(`ssh-host-${i}`, t, "Add", () => addHost(h.alias), { enabled: h.ok }))));
}

export function machinesTab(panel, t) {
  return v_flex().w_full().flex_1().min_h(0).gap(4).overflow_y_scrollbar()
    .when(!!panel.machinesProblem, (el) => el.child(div().px(8).flex_none().child(note(t, panel.machinesProblem, t.blocked))))
    .children((panel.machines ?? []).map((m, i) => machineSection(panel, t, m, i)))
    .when(panel.sshHosts !== false, (el) => el.child(hostsSection(panel, t)));
}
