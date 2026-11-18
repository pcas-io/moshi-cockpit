// "+ Agent" → Machines: a matrix of this Mac and Herdr's machines (rows) by the six harnesses (columns,
// `machine inventory`), then the add area: the ssh hosts not added yet and the saved hosts as chips, and a field for
// a typed target. A chip or the field starts the add flow inline (host key check, Trust with the fingerprint,
// `machine add --batch`, which installs herdr-core there); a password, keys only or ssh settings the dialog doesn't
// handle get "In a tab" (`machine add` attached).
// Opening the tab asks once; "Refresh" asks one machine again; nothing polls. A Sign in or Install cell opens a
// tab where the command runs (on that machine for a remote one). Fixed row height and column widths, so This Mac,
// a few machines and the add area fit the 640×460 dialog without scrolling. The logic lives in machines.js (tested
// with node); `panel` is the MeshAgents dialog, which keeps the state.

import { div } from "gpui-kit";
import { v_flex, h_flex, InputState, Input } from "gpui-base";
import { exec, home, run, props, closeOverlay, spaces, machines } from "herdr";
import { ago, btn, cockpit, plain } from "./util.js";
import { ADD_EXEC, ADD_NOTE, COLUMNS, GLYPH, HOSTKEY_EXEC, IDLE, INVENTORY_EXEC, LEGEND, TRUST_EXEC, addBatchArgs, addHostCommand,
         busy, cellAction, flowView, hostRows, hostkeyArgs, hostsRemoveArgs, inventoryArgs, layoutReady, machineRows, matrixRow,
         nextStep, tabCommand, trustArgs, typedTarget } from "./machines.js";

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

// Herdr's machines and the hosts to add. A connected machine without an inventory yet is asked, all in parallel;
// `seed` is the inventory a just added machine came with (`machine add --batch`), so it is not asked again.
function loadList(panel, cx, seed = null) {
  cx.spawn(async (cx) => {
    try {
      const list = await cockpit(exec, home, ["machine", "list", "--json"]);
      panel.machines = machineRows(list);
      panel.sshHosts = hostRows(list?.ssh_hosts);
      panel.savedHosts = hostRows(list?.saved_hosts);
      panel.machinesProblem = list?.problem ? plain(list.problem, 300) : null;
      if (seed?.name && Array.isArray(seed.data?.harnesses)) {
        panel.inventory[keyOf(seed.name)] = { loading: false, data: seed.data, error: null };
      }
      for (const m of panel.machines) if (m.name && m.connected && !panel.inventory[keyOf(m.name)]) inspect(panel, m.name, false, cx);
    } catch (e) {
      panel.sshHosts = panel.savedHosts = [];  // unknown: no chips rather than stale ones
      panel.machinesProblem = plain(e?.message ?? e, 300);
    }
    cx.notify();
  });
}

// This Mac is asked at once; Herdr's machines as soon as the list says which are connected.
export function loadMachines(panel, cx) {
  Object.assign(panel, { machines: machineRows(null), sshHosts: null, savedHosts: null, machinesProblem: null,
                         inventory: {}, machineAsk: null, targetNote: "" });
  panel.flow ??= IDLE;
  if (!panel.machineTarget) {  // retained text state: created once, here (init or a click), never in render
    panel.machineTarget = InputState.new({ placeholder: "user@host, user@host:port or an ssh alias" });
    panel.machineTarget.on("submit", (_e, cx) => addTyped(panel, cx));
  }
  inspect(panel, null, false, cx);
  loadList(panel, cx);
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

// The fallback: `machine add` attached in a tab on this Mac, where hc and ssh ask what they need.
function addInTab(alias) {
  const next = addHostCommand(spaces(), cli(), alias);
  run(next.command, next.args);
  closeOverlay(props().overlay);
}

// One step of the add flow (machines.js nextStep); entering "adding" runs `machine add --batch`, and its success
// clears the field and reloads the list with the new machine's inventory from the answer.
function advance(panel, answer, cx) {
  const before = panel.flow;
  panel.flow = nextStep(before, answer);
  cx.notify();
  if (panel.flow === before) return;
  if (panel.flow.step === "adding") addNow(panel, panel.flow.target, cx);
  if (answer.event === "added" && answer.data?.ok === true) {
    panel.machineTarget?.set_value("");
    panel.targetNote = "";
    loadList(panel, cx, { name: answer.data.machine, data: answer.data.inventory });
  }
}

function ask(panel, target, event, call, cx) {
  cx.spawn(async (cx) => {
    let answer;
    try {
      answer = { event, target, data: await call() };
    } catch (e) {
      answer = { event: "error", target, message: plain(e?.message ?? e, 300) };
    }
    advance(panel, answer, cx);
  });
}

function startFlow(panel, target, cx) {
  if (busy(panel.flow)) return;
  advance(panel, { event: "start", target }, cx);
  ask(panel, target, "hostkey", () => cockpit(exec, home, hostkeyArgs(target), true, HOSTKEY_EXEC), cx);
}

function trustNow(panel, cx) {
  if (panel.flow?.step !== "trust") return;  // a second click while it runs
  const { target, key } = panel.flow;
  advance(panel, { event: "trust", target }, cx);
  ask(panel, target, "trusted", () => cockpit(exec, home, trustArgs(target, key), true, TRUST_EXEC), cx);
}

function addNow(panel, target, cx) {
  ask(panel, target, "added", () => cockpit(exec, home, addBatchArgs(target), true, ADD_EXEC), cx);
}

function removeSaved(panel, alias, cx) {
  cx.spawn(async (cx) => {
    try {
      await cockpit(exec, home, hostsRemoveArgs(alias));
      panel.savedHosts = (panel.savedHosts ?? []).filter((h) => h.alias !== alias);
    } catch (e) {
      panel.machinesProblem = plain(e?.message ?? e, 300);
    }
    cx.notify();
  });
}

// "Add a machine": what passes starts the flow like a chip; anything else shows the note and runs nothing.
function addTyped(panel, cx) {
  const target = typedTarget(panel.machineTarget.value());
  panel.targetNote = target.note;
  if (target.ok) return startFlow(panel, target.value, cx);
  cx.notify();
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

// A host chip starts the add flow (not while a step runs); a saved one has a × that drops it from the list.
function hostChip(panel, t, h, id, removable) {
  const el = h_flex().id(id).h(20).max_w(200).px(7).gap(6).items_center().rounded(5).border_1().border_color(t.border)
    .text_size(11.5).text_color(h.ok ? t.foreground : t.dim);
  if (!h.ok) return el.border_color(t.dim).tooltip(`${h.label}: ${h.note}`).child(div().truncate().child(`${h.label} \u00b7 ${h.note}`));
  const idle = !busy(panel.flow);
  return el.child(div().id(`${id}-add`).min_w_0().truncate().tooltip(`Add ${h.label}`)
      .when(idle, (e) => e.cursor_pointer().hover((s) => s.text_color(t.accent)).on_click((_e, cx) => startFlow(panel, h.alias, cx)))
      .child(`+ ${h.label}`))
    .when(removable, (e) => e.child(div().id(`${id}-remove`).flex_none().text_color(t.dim).cursor_pointer().tooltip(`Forget ${h.label}`)
      .hover((s) => s.text_color(t.foreground)).on_click((_e, cx) => removeSaved(panel, h.alias, cx)).child("\u00d7")));
}

// One wrapping row per list, its label first; no row for an empty list.
function chipRow(panel, t, label, hosts, prefix, removable) {
  const list = Array.isArray(hosts) ? hosts : [];
  if (list.length === 0) return null;
  return h_flex().w_full().flex_wrap().gap_x(6).gap_y(4).px(6).items_center().flex_none()
    .child(div().h(20).flex().items_center().text_size(10.5).text_color(t.dim).child(label))
    .children(list.map((h, i) => hostChip(panel, t, h, `${prefix}-${i}`, removable)));
}

// One 28 px row under the chips (there even when the list failed): a target typed here starts the flow (Enter or Add).
function addRow(panel, t) {
  return h_flex().w_full().h(28).gap(6).px(6).items_center().flex_none()
    .child(div().flex_none().text_size(11.5).text_color(t.dim).child("Add a machine"))
    .child(Input.new(panel.machineTarget).flex_1().min_w_0().h(26).px(8).rounded(6).border_1().border_color(t.input)
      .bg(t.background).text_size(11.5))
    .child(btn("machine-add", t, "Add", (_e, cx) => addTyped(panel, cx), { enabled: !busy(panel.flow) }))
    .when(!!panel.targetNote, (el) => el.child(div().flex_none().text_size(11).text_color(t.dim).child(panel.targetNote)));
}

// The add flow's one area under the field: what runs, the Trust question or what went wrong.
function flowArea(panel, t) {
  const view = flowView(panel.flow);
  if (!view) return null;
  const color = view.tone === "blocked" ? t.blocked : view.tone === "foreground" ? t.foreground : t.dim;
  const cancel = (_e, cx) => advance(panel, { event: "cancel" }, cx);
  const buttons = {
    trust: () => btn("machine-trust", t, "Trust", (_e, cx) => trustNow(panel, cx), { primary: true }),
    cancel: () => btn("machine-trust-no", t, "Cancel", cancel),
    tab: () => btn("machine-add-tab", t, "In a tab", () => addInTab(panel.flow.target)),
  };
  return h_flex().w_full().gap(8).px(8).py(4).rounded(6).items_center().flex_none()
    .when(view.buttons.length > 0, (e) => e.bg(t.raised))
    .child(div().flex_1().min_w_0().text_size(11.5).text_color(color).child(view.text))
    .children(view.buttons.map((b) => buttons[b]()));
}

export function machinesTab(panel, t) {
  const ask = askRow(panel, t);
  const ssh = chipRow(panel, t, "From ~/.ssh/config", panel.sshHosts, "ssh-host", false);
  const saved = chipRow(panel, t, "Saved", panel.savedHosts, "saved-host", true);
  const flow = flowArea(panel, t);
  // line_height: one fixed text height for the whole tab, so its rows add up the same on every theme.
  return v_flex().w_full().flex_1().min_h(0).gap(6).line_height(1.2).overflow_y_scrollbar()
    .when(!!panel.machinesProblem, (el) => el.child(div().px(6).flex_none().child(note(t, panel.machinesProblem, t.blocked))))
    .child(legend(t))
    .child(v_flex().w_full().flex_none()
      .child(headerRow(t))
      .children((panel.machines ?? []).map((m, i) => machineRow(panel, t, m, i))))
    .when(!!ask, (el) => el.child(ask))
    .child(div().id("machines-add-note").px(6).flex_none().text_size(10.5).text_color(t.dim).truncate().tooltip(ADD_NOTE)
      .child(ADD_NOTE))
    .when(!!ssh, (el) => el.child(ssh))
    .when(!!saved, (el) => el.child(saved))
    .child(addRow(panel, t))
    .when(!!flow, (el) => el.child(flow));
}
