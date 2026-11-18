// "+ Agent" → Machines: a matrix of this Mac and Herdr's machines (rows) by the six harnesses (columns,
// `machine inventory`), then the add area: the ssh hosts not added yet and the saved hosts as chips, and a field for
// a typed target. A chip or the field starts the add flow inline (host key check, Trust with the fingerprint,
// `machine add --batch`, which installs herdr-core there); a password, keys only or ssh settings the dialog doesn't
// handle get "In a tab" (`machine add` attached).
// Opening the tab asks once; "Refresh" asks one machine again; nothing polls: a machine that was not connected is
// asked again when Herdr reports its layout ready (machines(), which re-renders the dialog). A Sign in or Install
// cell opens a tab where the command runs (on that machine for a remote one); "×" at a row's end removes that
// machine from Herdr after an inline question. Fixed row height and column widths, so This Mac, six machines and
// the add area fit the 640×460 dialog without scrolling. An add's outcome is kept in localStorage (ADDS_KEY) for a
// dialog Herdr closed while it ran. A machine Herdr warns about says so in its row with the action that fixes it:
// "Log in" (`hc machine login` in a tab on this Mac) or "Fix" (linger, after an inline question, in a tab there);
// a warning in the spaces list opens this tab with that machine's row highlighted (props().focus). The logic lives in
// machines.js (tested with node); `panel` is the MeshAgents dialog, which keeps the state.

import { div } from "gpui-kit";
import { v_flex, h_flex, InputState, Input } from "gpui-base";
import { exec, home, run, props, closeOverlay, spaces, machines } from "herdr";
import { ago, btn, cockpit, plain } from "./util.js";
import { ADDS_KEY, ADD_EXEC, ADD_NOTE, COLUMNS, GLYPH, HOSTKEY_EXEC, IDLE, INVENTORY_EXEC, LEGEND, REMOVE_EXEC, TRUST_EXEC,
         addBatchArgs, addEnded, addHostCommand, addStarted, busy, cellAction, cleanAdds, fixCommand, flowView, focusName,
         hostRows, hostkeyArgs, hostsRemoveArgs, inventoryArgs, layoutReady, lingerCommand, lingerQuestion, loginCommand,
         machineRemoveArgs, machineRows, matrixRow, nextStep, recentNames, removeQuestion, tabCommand, takeOutcome, trustArgs,
         typedTarget } from "./machines.js";

const cli = () => `${home()}/.local/bin/moshi-cockpit`;
const hc = () => `${home()}/.local/bin/herdr-core`;
const keyOf = (name) => name ?? "";
const ROW_H = 34;  // every machine row, whatever it shows: a 14 px name line, 4 px apart from a 12 px status line
const CELL_W = 46;  // every harness column; the machine column takes the rest
const REMOVE_W = 20;  // the "×" column at the row end

// The add flow's outcomes (machines.js cleanAdds); localStorage may be missing or refuse: then nothing is kept.
function loadAdds() {
  try { return cleanAdds(JSON.parse(localStorage.getItem(ADDS_KEY) ?? "{}"), Date.now()); } catch { return {}; }
}

function keepAdds(panel, change) {
  panel.adds = change(loadAdds(), Date.now());  // read again: another dialog may have written since
  try { localStorage.setItem(ADDS_KEY, JSON.stringify(panel.adds)); } catch { /* kept for this dialog only */ }
}

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
      panel.machines = machineRows(list, recentNames(panel.adds, list, Date.now()));
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

// This Mac is asked at once; Herdr's machines as soon as the list says which are connected. The last add's outcome,
// left by a dialog that closed while it ran, shows once in the flow area.
export function loadMachines(panel, cx) {
  Object.assign(panel, { machines: machineRows(null), sshHosts: null, savedHosts: null, machinesProblem: null,
                         inventory: {}, machineAsk: null, removeAsk: null, lingerAsk: null, targetNote: "", rechecked: new Set() });
  panel.focus = focusName(props());  // the machine a warning in the spaces list opened this tab for
  panel.flow ??= IDLE;
  let shown = null;
  keepAdds(panel, (adds) => {
    const taken = takeOutcome(adds);
    shown = taken.flow;
    return taken.adds;
  });
  if (shown && !busy(panel.flow)) panel.flow = shown;
  if (!panel.machineTarget) {  // retained text state: created once, here (init or a click), never in render
    panel.machineTarget = InputState.new({ placeholder: "user@host, user@host:port or an ssh alias" });
    panel.machineTarget.on("submit", (_e, cx) => addTyped(panel, cx));
  }
  inspect(panel, null, false, cx);
  loadList(panel, cx);
}

// The dialog closes (MeshAgents.close, where Herdr calls it): an answer that still arrives is stored for the next
// Machines tab instead of being shown here. Without the call, a running add's pending mark says so next time.
export function closeMachines(panel) {
  panel.closed = true;
}

export function refreshMachine(panel, name, cx) {
  if (panel.inventory?.[keyOf(name)]?.loading) return;
  panel.machineAsk = null;
  inspect(panel, name, true, cx);
  cx.notify();
}

// A machine not connected yet: Herdr's list is read again, and a machine connected by now is asked.
function recheck(panel, name, cx) {
  panel.rechecked.add(name);
  loadList(panel, cx);
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
  panel.removeAsk = panel.lingerAsk = null;
  cx.notify();
}

function askRemove(panel, m, cx) {
  if (panel.removeAsk?.busy) return;
  panel.removeAsk = { machine: m.name, busy: false };
  panel.machineAsk = panel.lingerAsk = null;
  cx.notify();
}

// A warning's action: "Log in" opens its tab at once (hc asks there; the row's Refresh checks again afterwards),
// "Fix" asks inline first (it runs with sudo there).
function warningAct(panel, m, action, cx) {
  if (action.kind === "login") {
    const next = loginCommand(spaces(), hc(), m.name);
    if (!next) return;
    run(next.command, next.args);
    closeOverlay(props().overlay);
    return;
  }
  if (panel.removeAsk?.busy) return;
  panel.lingerAsk = { machine: m.name, user: action.user, copied: false };
  panel.machineAsk = panel.removeAsk = null;
  cx.notify();
}

// "Run in a tab": the fix in a held tab on that machine, where sudo asks for the password; the app never sees it.
function fixInTab(m, ask) {
  const next = fixCommand(spaces(), m.name, ask.user);
  if (!next) return;
  run(next.command, next.args);
  closeOverlay(props().overlay);
}

// `machine remove`: Herdr forgets it, its cached inventory goes; then the list is read again.
function removeNow(panel, cx) {
  const ask = panel.removeAsk;
  if (!ask || ask.busy) return;
  ask.busy = true;
  cx.notify();
  cx.spawn(async (cx) => {
    try {
      await cockpit(exec, home, machineRemoveArgs(ask.machine), true, REMOVE_EXEC);
      delete panel.inventory[keyOf(ask.machine)];
      loadList(panel, cx);
    } catch (e) {
      panel.machinesProblem = plain(e?.message ?? e, 300);  // the list stays as it was
    }
    if (panel.removeAsk === ask) panel.removeAsk = null;
    cx.notify();
  });
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
  if (panel.flow.step === "adding") {
    keepAdds(panel, (adds, now) => addStarted(adds, panel.flow.target, now));
    addNow(panel, panel.flow.target, cx);
  }
  if (before.step === "adding" && (answer.event === "added" || answer.event === "error")) {
    // shown here at once: its stored mark keeps only time and machine (for "Connecting"), no outcome to show again
    keepAdds(panel, (adds, now) => addEnded(adds, before.target, panel.flow, answer.data?.machine, now, !panel.closed));
  }
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

// A warning's action, as small as Refresh: "Log in" or "Fix".
function warningLink(panel, t, m, action, id) {
  const asking = panel.lingerAsk?.machine === m.name;
  return div().id(id).flex_none().px(3).rounded(3).text_color(t.foreground).cursor_pointer().when(asking, (e) => e.bg(t.raised))
    .hover((s) => s.bg(t.hover)).on_click((_e, cx) => warningAct(panel, m, action, cx)).child(action.label);
}

// The cell glyphs' legend; main.js puts it beside the tab buttons.
export function machinesLegend(t) {
  return h_flex().h(14).flex_none().gap(10).items_center().text_size(10.5).text_color(t.dim)
    .children(LEGEND.map((l) => h_flex().gap(4).items_center()
      .child(div().text_color(glyphColor(t, l.show)).child(GLYPH[l.show])).child(l.text)));
}

function headerRow(t) {
  return h_flex().w_full().h(16).flex_none().items_center().text_size(10.5).text_color(t.dim)
    .child(div().flex_1().min_w_0().px(6).child("Machine"))
    .children(COLUMNS.map((c) => div().w(CELL_W).flex_none().text_center().truncate().child(c.short)))
    .child(div().w(REMOVE_W).flex_none());
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

// A connected machine's warning takes its status line (the whole of it and the status in the tooltip).
function machineCell(panel, t, m, r, id) {
  const refresh = (_e, cx) => (m.connected ? refreshMachine(panel, m.name, cx) : recheck(panel, m.name, cx));
  const warn = m.connected ? r.warning : null;
  const status = warn ? [warn.note, r.status].filter(Boolean).join(" \u00b7 ") : r.status;
  return v_flex().flex_1().min_w_0().h(ROW_H).px(6).gap(4).justify_center()
    .child(div().text_size(12.5).line_height("14px").font_semibold().text_color(m.connected ? t.foreground : t.dim).truncate()
      .child(m.label))
    .when(r.refresh, (el) => el.child(h_flex().w_full().min_w_0().gap(6).items_center().text_size(10).line_height("12px")
      .child(div().id(`${id}-status`).flex_1().min_w_0().text_color(warn ? t.blocked : t.dim).truncate()
        .when(!!status, (e) => e.tooltip(status)).child(warn ? warn.note : r.status))
      .when(!!warn?.action, (e) => e.child(warningLink(panel, t, m, warn?.action, `${id}-warning`)))
      .child(div().id(`${id}-refresh`).flex_none().rounded(3).text_color(r.loading ? t.dim : t.muted)
        .when(!r.loading, (e) => e.cursor_pointer().hover((s) => s.text_color(t.foreground))
          .on_click(refresh))
        .child("Refresh"))));
}

// The row's end: "×" for a machine of Herdr's (never This Mac, never a name hc could misread), else the same gap.
function removeCell(panel, t, m, id) {
  const el = div().id(id).w(REMOVE_W).h(ROW_H).flex_none().flex().items_center().justify_center().rounded(5);
  if (!m.removable) return el;
  const asking = panel.removeAsk?.machine === m.name;
  return el.text_size(13).text_color(asking ? t.foreground : t.dim).cursor_pointer().tooltip(`Remove ${m.label} from Herdr`)
    .when(asking, (e) => e.bg(t.raised)).hover((s) => s.text_color(t.foreground).bg(t.hover))
    .on_click((_e, cx) => askRemove(panel, m, cx)).child("\u00d7");
}

// One machine, one fixed-height row: the machine cell, then six cells or one line (a note or an error) across them,
// then "×". The connecting explanation may take two lines (the whole of it in the tooltip).
function machineRow(panel, t, m, index, cx) {
  const ready = layoutReady(machines(), m.name);  // a tab opens there only once Herdr can
  // Herdr has the layout of a machine the list did not show connected: read the list again, once per dialog
  if (!m.connected && m.removable && ready && !panel.rechecked?.has(m.name)) recheck(panel, m.name, cx);
  const inv = m.connected ? panel.inventory?.[keyOf(m.name)] : null;  // never an unsupported name's key
  const r = matrixRow(m, inv, ready, ago);
  const id = `machine-${index}`;
  const line = r.error || r.note;
  const harnesses = inv?.data?.harnesses ?? [];
  const warn = !m.connected && !r.error ? r.warning : null;  // not connected: the warning is the note
  const text = warn ? h_flex().w_full().min_w_0().gap(8).items_center().child(div().flex_1().min_w_0().child(note(t, line, t.blocked)))
      .when(!!warn.action, (e) => e.child(warningLink(panel, t, m, warn.action, `${id}-warning`).text_size(11)))
    : m.connecting && !r.error
      ? div().text_size(10.5).line_height("12px").max_h(24).overflow_hidden().text_color(t.dim).child(line)
      : note(t, line, r.error ? t.blocked : t.dim);
  return h_flex().id(id).w_full().h(ROW_H).flex_none().items_center()
    .when(panel.focus != null && panel.focus === m.name, (e) => e.rounded(6).bg(t.selected))
    .child(machineCell(panel, t, m, r, id))
    .when(r.cells.length > 0, (el) => el.children(r.cells.map((c) =>
      cell(panel, t, m, harnesses.find((h) => h?.name === c.name), c, `${id}-${c.name}`))))
    .when(r.cells.length === 0, (el) => el.child(div().id(`${id}-note`).w(CELL_W * COLUMNS.length).flex_none().px(6)
      .when(!!line, (e) => e.tooltip(line)).child(text)))
    .child(removeCell(panel, t, m, `${id}-remove`));
}

// The question to remove a machine, in the same place: Remove or Cancel.
function removeRow(panel, t) {
  const ask = panel.removeAsk;
  const m = (panel.machines ?? []).find((k) => k.removable && k.name === ask.machine);
  if (!m) return null;
  return h_flex().w_full().gap(8).px(8).py(4).rounded(6).bg(t.raised).items_center().flex_none()
    .child(div().flex_1().min_w_0().text_size(11.5).text_color(t.foreground)
      .child(ask.busy ? `Removing ${m.label}\u2026` : removeQuestion(m.label)))
    .child(btn("machine-remove-yes", t, "Remove", (_e, cx) => removeNow(panel, cx), { enabled: !ask.busy }))
    .child(btn("machine-remove-no", t, "Cancel", (_e, cx) => { panel.removeAsk = null; cx.notify(); }, { enabled: !ask.busy }));
}

// The question before the disconnect fix, in the same place: what it runs where, then Run in a tab, Copy or Cancel.
// Its text takes the width (up to three lines), the buttons go under it; "Run in a tab" once Herdr has that
// machine's layout.
function lingerRow(panel, t) {
  const ask = panel.lingerAsk;
  const m = (panel.machines ?? []).find((k) => k.name === ask.machine && k.warning?.action?.kind === "linger"
    && k.warning.action.user === ask.user);
  if (!m) return null;
  const command = lingerCommand(ask.user);
  return v_flex().w_full().gap(4).px(8).py(4).rounded(6).bg(t.raised).flex_none()
    .child(div().w_full().text_size(11.5).text_color(t.foreground).child(lingerQuestion(m.label, ask.user)))
    .child(h_flex().gap(8).items_center()
      .child(btn("machine-fix-yes", t, "Run in a tab", () => fixInTab(m, ask), { primary: true, enabled: layoutReady(machines(), m.name) }))
      .child(btn("machine-fix-copy", t, ask.copied ? "Copied" : "Copy",
        (_e, cx) => { cx.write_to_clipboard(command); ask.copied = true; cx.notify(); }))
      .child(btn("machine-fix-no", t, "Cancel", (_e, cx) => { panel.lingerAsk = null; cx.notify(); })));
}

// The confirm for a remote install, under the matrix: the vendor's command, then Install or Cancel.
function askRow(panel, t) {
  if (panel.lingerAsk) return lingerRow(panel, t);
  if (panel.removeAsk) return removeRow(panel, t);
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
// Why here and not in Herdr's own form is the label's tooltip.
function addRow(panel, t) {
  return h_flex().w_full().h(28).gap(6).px(6).items_center().flex_none()
    .child(div().id("machines-add-note").flex_none().text_size(11.5).text_color(t.dim).tooltip(ADD_NOTE).child("Add a machine"))
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

// Height budget, 356 px for the tab (the legend sits beside the tab buttons): header 16 + This Mac and 6 machines × 34
// = 254, a chip row 20, the add row 28, gaps 6 → 314; with the install or remove question (34) 354, or with a flow
// line (22) 342. Question and flow line together fit without chips (356). The fix question (up to 3 lines of 14 +
// buttons 24 + 12 = 78) stands alone under the matrix: 254 + 6 + 78 = 338; the add area is back after it.
export function machinesTab(panel, t, cx) {
  const ask = askRow(panel, t);
  const fixing = !!panel.lingerAsk && !!ask;
  const ssh = fixing ? null : chipRow(panel, t, "From ~/.ssh/config", panel.sshHosts, "ssh-host", false);
  const saved = fixing ? null : chipRow(panel, t, "Saved", panel.savedHosts, "saved-host", true);
  const flow = fixing ? null : flowArea(panel, t);
  // line_height: one fixed text height for the whole tab, so its rows add up the same on every theme.
  return v_flex().w_full().flex_1().min_h(0).gap(6).line_height(1.2).overflow_y_scrollbar()
    .when(!!panel.machinesProblem, (el) => el.child(div().px(6).flex_none().child(note(t, panel.machinesProblem, t.blocked))))
    .child(v_flex().w_full().flex_none()
      .child(headerRow(t))
      .children((panel.machines ?? []).map((m, i) => machineRow(panel, t, m, i, cx))))
    .when(!!ask, (el) => el.child(ask))
    .when(!!ssh, (el) => el.child(ssh))
    .when(!!saved, (el) => el.child(saved))
    .when(!fixing, (el) => el.child(addRow(panel, t)))
    .when(!!flow, (el) => el.child(flow));
}
