// "+ Agent" → Machines: a matrix of this Mac and Herdr's machines (rows) by the six harnesses (columns,
// `machine inventory`), and the ssh hosts not added yet as chips (`machine add` in a visible tab). Opening the tab
// asks once; "Refresh" asks one machine again; nothing polls. A Sign in or Install cell opens a tab where the
// command runs (on that machine for a remote one). Fixed row height and column widths, so This Mac, six machines
// and the hosts fit the 640×460 dialog without scrolling. The logic lives in machines.js (tested with node);
// `panel` is the MeshAgents dialog, which keeps the state.

import { div } from "gpui-kit";
import { v_flex, h_flex } from "gpui-base";
import { exec, home, run, props, closeOverlay, spaces, machines } from "herdr";
import { ago, btn, cockpit, plain } from "./util.js";
import { COLUMNS, GLYPH, INVENTORY_EXEC, LEGEND, addHostCommand, cellAction, hostRows, inventoryArgs, layoutReady, machineRows,
         matrixRow, tabCommand } from "./machines.js";

const cli = () => `${home()}/.local/bin/moshi-cockpit`;
const keyOf = (name) => name ?? "";
const ROW_H = 26;  // every machine row, whatever it shows: a 14 px name line over a 12 px status line
const CELL_W = 46;  // every harness column; the machine column takes the rest

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

const glyphColor = (t, show) => (show === "ready" ? t.ansi2 ?? t.done : show === "sign-in" ? t.foreground : t.muted);

function note(t, text, color = t.dim) {
  return div().text_size(11).text_color(color).truncate().child(text);
}

function legend(t) {
  return h_flex().h(14).flex_none().gap(12).px(6).items_center().text_size(10.5).text_color(t.dim)
    .children(LEGEND.map((l) => h_flex().gap(4).items_center()
      .child(div().text_color(glyphColor(t, l.show)).child(GLYPH[l.show])).child(l.text)));
}

function headerRow(t) {
  return h_flex().w_full().h(16).flex_none().items_center().text_size(10.5).text_color(t.dim)
    .child(div().flex_1().min_w_0().px(6).child("Machine"))
    .children(COLUMNS.map((c) => div().w(CELL_W).flex_none().text_center().truncate().child(c.short)));
}

// A cell: ● ready, ◐ sign in, ○ install, or empty; the state and what a click does in its tooltip.
function cell(panel, t, m, h, c, id) {
  const el = div().id(id).w(CELL_W).h(ROW_H).flex_none().flex().items_center().justify_center().rounded(5).text_size(13)
    .when(!!c.hint, (e) => e.tooltip(c.hint));
  if (!c.show) return el;
  const glyph = el.text_color(glyphColor(t, c.show)).child(GLYPH[c.show]);
  if (!c.action.kind) return glyph;
  const asking = panel.machineAsk?.machine === m.name && panel.machineAsk?.harness === c.name;
  return glyph.cursor_pointer().when(asking, (e) => e.bg(t.raised)).hover((s) => s.bg(t.hover))
    .on_click((_e, cx) => act(panel, m, h, c.action, cx));
}

function machineCell(panel, t, m, r, id) {
  return v_flex().flex_1().min_w_0().h(ROW_H).px(6).justify_center()
    .child(div().text_size(12.5).line_height("14px").font_semibold().text_color(m.connected ? t.foreground : t.dim).truncate()
      .child(m.label))
    .when(r.refresh, (el) => el.child(h_flex().w_full().min_w_0().gap(6).items_center().text_size(10).line_height("12px")
      .child(div().id(`${id}-status`).flex_1().min_w_0().text_color(t.dim).truncate().when(!!r.status, (e) => e.tooltip(r.status))
        .child(r.status))
      .child(div().id(`${id}-refresh`).flex_none().rounded(3).text_color(r.loading ? t.dim : t.muted)
        .when(!r.loading, (e) => e.cursor_pointer().hover((s) => s.text_color(t.foreground))
          .on_click((_e, cx) => refreshMachine(panel, m.name, cx)))
        .child("Refresh"))));
}

// One machine, one fixed-height row: the machine cell, then six cells or one line (a note or an error) across them.
function machineRow(panel, t, m, index) {
  const inv = m.connected ? panel.inventory?.[keyOf(m.name)] : null;  // never an unsupported name's key
  const r = matrixRow(m, inv, layoutReady(machines(), m.name), ago);  // a tab opens there only once Herdr can
  const id = `machine-${index}`;
  const line = r.error || r.note;
  const harnesses = inv?.data?.harnesses ?? [];
  return h_flex().id(id).w_full().h(ROW_H).flex_none().items_center()
    .child(machineCell(panel, t, m, r, id))
    .when(r.cells.length > 0, (el) => el.children(r.cells.map((c) =>
      cell(panel, t, m, harnesses.find((h) => h?.name === c.name), c, `${id}-${c.name}`))))
    .when(r.cells.length === 0, (el) => el.child(div().id(`${id}-note`).w(CELL_W * COLUMNS.length).flex_none().px(6)
      .when(!!line, (e) => e.tooltip(line)).child(note(t, line, r.error ? t.blocked : t.dim))));
}

// The confirm for a remote install, under the matrix: the vendor's command, then Install or Cancel.
function askRow(panel, t) {
  const ask = panel.machineAsk;
  const m = ask && (panel.machines ?? []).find((k) => k.connected && k.name === ask.machine);
  const h = m && (panel.inventory?.[keyOf(m.name)]?.data?.harnesses ?? []).find((k) => k?.name === ask.harness);
  const action = h ? cellAction(h, m.name).action : null;
  if (!action?.confirm || !layoutReady(machines(), m.name)) return null;
  return h_flex().w_full().gap(8).px(8).py(4).rounded(6).bg(t.raised).items_center().flex_none()
    .child(div().flex_1().min_w_0().text_size(11.5).text_color(t.foreground).child(plain(action.confirm, 400)))
    .child(btn("machine-ask-yes", t, "Install", () => openTab(m.name, plain(h.label, 40), action), { primary: true }))
    .child(btn("machine-ask-no", t, "Cancel", (_e, cx) => { panel.machineAsk = null; cx.notify(); }));
}

function hostChip(t, h, i) {
  const el = div().id(`ssh-host-${i}`).h(20).max_w(200).px(7).flex().items_center().rounded(5).border_1().border_color(t.border)
    .text_size(11.5).text_color(h.ok ? t.foreground : t.dim).truncate();
  if (!h.ok) return el.border_color(t.dim).tooltip(`${h.label}: ${h.note}`).child(`${h.label} \u00b7 ${h.note}`);
  return el.cursor_pointer().hover((s) => s.bg(t.hover)).tooltip(`Add ${h.label}`).on_click(() => addHost(h.alias))
    .child(`+ ${h.label}`);
}

function hostsSection(panel, t) {
  const hosts = panel.sshHosts;
  const list = Array.isArray(hosts) ? hosts : [];
  return v_flex().w_full().gap(4).px(6).flex_none()
    .child(div().h(14).text_size(10.5).text_color(t.dim).child("From ~/.ssh/config"))
    .when(hosts === null, (el) => el.child(note(t, "Checking…")))
    .when(Array.isArray(hosts) && hosts.length === 0, (el) => el.child(note(t, "No new hosts in ~/.ssh/config.")))
    .when(list.length > 0, (el) => el.child(h_flex().w_full().flex_wrap().gap_x(6).gap_y(4)
      .children(list.map((h, i) => hostChip(t, h, i)))));
}

export function machinesTab(panel, t) {
  const ask = askRow(panel, t);
  // line_height: one fixed text height for the whole tab, so its rows add up the same on every theme.
  return v_flex().w_full().flex_1().min_h(0).gap(6).line_height(1.2).overflow_y_scrollbar()
    .when(!!panel.machinesProblem, (el) => el.child(div().px(6).flex_none().child(note(t, panel.machinesProblem, t.blocked))))
    .child(legend(t))
    .child(v_flex().w_full().flex_none()
      .child(headerRow(t))
      .children((panel.machines ?? []).map((m, i) => machineRow(panel, t, m, i))))
    .when(!!ask, (el) => el.child(ask))
    .when(panel.sshHosts !== false, (el) => el.child(hostsSection(panel, t)));
}
