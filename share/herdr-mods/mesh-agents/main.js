// "Manage agents…" and "+ Agent" from the "+" menu and the sidebar: one dialog in the style of herdr's settings,
// the agents on the left, the selected one or "+ Agent" (tabs New, From moshi and Machines) on the right. New on
// another machine only starts a harness there in a new space: nothing goes to moshi or agents.json.
// Everything goes through `moshi-cockpit`: this mod never sees a token. Exit 3 (approval missing or expired) opens
// the connect dialog; installing a harness and connecting ask in a terminal tab. The logic lives in rules.js,
// catalog.js, harnesses.js and machines.js (tested with node), the Machines tab in machines-view.js; this file wires
// the views.

import { View, div } from "gpui-kit";
import { v_flex, h_flex, InputState, Input } from "gpui-base";
import { exec, home, theme, run, props, closeOverlay, openOverlay, notify, knownAgents, shared, machines } from "herdr";
import { btn, cockpit, plain } from "./util.js";
import { addArgs, checkName, checkSkill, loadArgs, outcome, removeArgs, roleText, setArgs } from "./rules.js";
import { harnessRows } from "./harnesses.js";
import { catalogRows, settle } from "./catalog.js";
import { loadSessions, startKey, startView } from "./start.js";
import { connectKey, connectView, openConnect } from "./connect.js";
import { closeMachines, loadMachines, machinesLegend, machinesTab } from "./machines-view.js";
import { INVENTORY_EXEC, inventoryArgs, remoteChips, remoteStart, whereChoices } from "./machines.js";

const KNOWN = "new-menu.agents";  // mesh-new-menu keeps knownAgents() here, per machine ("" = this one)
const cli = () => `${home()}/.local/bin/moshi-cockpit`;

// A finished call, never a throw: a CLI that could not start reads like any other error.
async function call(args) {
  try {
    return await exec(cli(), args);
  } catch (e) {
    return { code: 1, stdout: "", stderr: String(e?.message ?? e) };
  }
}

export default class MeshAgents extends View {
  init(_props, cx) {
    this.view = ["new", "start", "connect"].includes(props()?.view) ? props().view : "manage";
    if (this.view === "connect") {
      this.handle = cx.focus_handle();
      this.handle.focus();
      return;
    }
    if (this.view === "start") {
      Object.assign(this, { agent: props().agent, harness: props().harness, run: props().run, terminal: props().terminal, label: props().label, space: props().space, folder: props().folder,
                            here: [], all: [], at: -1, loaded: false, error: null, handle: cx.focus_handle() });
      this.handle.focus();
      loadSessions(this, cx);
      return;
    }
    this.data = null;
    this.selected = null;
    this.confirming = false;
    this.busy = false;
    this.error = null;
    this.tab = ["moshi", "machines"].includes(props()?.tab) ? props().tab : "new";
    this.harness = null;
    this.problem = null;
    this.note = null;
    this.catalog = null;
    this.catalogProblem = null;
    this.loadProblem = null;
    this.machines = null;  // the Machines tab: machines-view.js keeps its state on this dialog
    this.where = null;  // New: null = this Mac, else a machine from whereChoices()
    this.remote = Object.create(null);  // New on another machine: {machine: {loading, rows, error, known}}
    this.remoteHarness = null;
    this.known = shared(KNOWN)?.[""] ?? null;
    this.name = InputState.new({ placeholder: "Name, e.g. research" });
    this.name.on("submit", (_e, cx) => this.create(cx));
    this.role = InputState.new({ placeholder: "Role and personality (optional)" });
    this.role.on("submit", (_e, cx) => this.create(cx));
    this.skill = InputState.new({ placeholder: "ocellio skill (optional), e.g. code-review" });
    this.skill.on("submit", (_e, cx) => this.create(cx));
    this.editRole = InputState.new({ placeholder: "Role and personality" });
    this.editSkill = InputState.new({ placeholder: "ocellio skill (optional)" });
    if (this.view === "new") {
      this.name.focus();
      if (this.tab === "moshi") this.loadCatalog(cx);
      if (this.tab === "machines") loadMachines(this, cx);
    }
    cx.spawn(async (cx) => {
      try {
        this.known = await knownAgents({});
      } catch { /* the CLI's own answer decides alone */ }
      cx.notify();
    });
    this.reload(cx, props()?.agent);
  }

  // Herdr closes the dialog: an add's answer that still arrives waits for the next Machines tab.
  close() {
    if (this.machines) closeMachines(this);
  }

  // From the session list to this agent's details: the dialog of "Manage agents…".
  manageAgent() {
    closeOverlay(props().overlay);
    openOverlay({ mod: "mesh-agents", kind: "dialog", width: 640, height: 460, props: { view: "manage", agent: this.agent } });
  }

  reload(cx, select) {
    cx.spawn(async (cx) => {
      try {
        this.data = await cockpit(exec, home, ["agent", "list", "--json"]);
        this.error = null;
        const names = (this.data.agents ?? []).map((a) => a.name);
        if (select && names.includes(select)) this.selected = select;
        else if (!names.includes(this.selected)) this.selected = names[0] ?? null;
        this.fillEdit();
      } catch (e) {
        this.error = plain(e?.message ?? e, 200);
      }
      cx.notify();
    });
  }

  fillEdit() {
    this.editRole.set_value(this.agent()?.role ?? "");
    this.editSkill.set_value(this.agent()?.skill ?? "");
  }

  agent() {
    return (this.data?.agents ?? []).find((a) => a.name === this.selected) ?? null;
  }

  pick(name, cx) {
    this.view = "manage";
    this.selected = name;
    this.confirming = false;
    this.note = null;
    this.fillEdit();
    cx.notify();
  }

  // "+ Agent": an empty form; what was typed is cleared only here, never after a refusal.
  showNew(cx) {
    this.view = "new";
    this.confirming = false;
    this.note = null;
    this.problem = null;
    this.harness = null;
    this.where = null;
    this.remoteHarness = null;
    this.name.set_value("");
    this.role.set_value("");
    this.skill.set_value("");
    this.showTab("new", cx);
  }

  showTab(tab, cx) {
    this.tab = tab;
    if (tab === "new") this.name.focus();
    if (tab === "moshi" && !this.catalog) this.loadCatalog(cx);
    if (tab === "machines" && !this.machines) loadMachines(this, cx);
    cx.notify();
  }

  loadCatalog(cx) {
    this.catalog = null;
    this.catalogProblem = null;
    cx.spawn(async (cx) => {
      const found = outcome(await call(["agent", "catalog", "--json"]));
      if (found.kind === "connect") return openConnect(props().overlay);
      if (found.kind === "ok") {
        try {
          this.catalog = catalogRows(JSON.parse(found.out).agents);
        } catch {
          this.catalogProblem = "moshi-cockpit gave no list";
        }
      } else {
        this.catalogProblem = found.message ?? "moshi-cockpit gave no list";
      }
      cx.notify();
    });
  }

  // After add or load: done → the agent's details; exit 3 → the connect dialog; else the problem stays inline.
  finish(next, name, verb, onProblem, cx) {
    if (next.action === "connect") return openConnect(props().overlay);
    if (next.action === "stay") return onProblem(next.problem);
    notify({ title: "Agents", body: `${name} ${verb}`, level: "success" });
    this.view = "manage";
    this.catalog = null;
    this.reload(cx, name);
  }

  // The machine New starts on: a picked one only while Herdr still has its layout ready, else this Mac.
  whereNow(choices = whereChoices(machines())) {
    return choices.some((c) => c.name === this.where) ? this.where : null;
  }

  pickWhere(name, cx) {
    this.where = name;
    this.remoteHarness = null;
    this.problem = null;
    if (name === null) this.name.focus();
    else if (!this.remote[name] || this.remote[name].error) this.loadRemote(name, cx);
    cx.notify();
  }

  // That machine's inventory (the CLI's cached call) and Herdr's own check there, side by side.
  loadRemote(name, cx) {
    const entry = { loading: true, rows: null, error: null, known: null };
    this.remote[name] = entry;
    cx.spawn(async (cx) => {
      const [inv, known] = await Promise.all([
        cockpit(exec, home, inventoryArgs(name, false), true, INVENTORY_EXEC).then(
          (d) => (Array.isArray(d?.harnesses) ? { rows: d.harnesses } : { error: "moshi-cockpit gave no inventory" }),
          (e) => ({ error: plain(e?.message ?? e, 300) })),
        knownAgents({ on: name }).catch(() => null),
      ]);
      Object.assign(entry, { loading: false, rows: inv.rows ?? null, error: inv.error ?? null, known });
      cx.notify();
    });
  }

  startRemote(machine, cx) {
    const entry = this.remote[machine];
    const chip = remoteChips(entry?.rows, machine, entry?.known).find((c) => c.name === this.remoteHarness && c.selectable);
    const row = chip && entry.rows.find((r) => r.name === chip.name);
    if (!row) { this.problem = "Choose a harness"; cx.notify(); return; }
    run("space.new", remoteStart(machine, row, entry.known));
    closeOverlay(props().overlay);
  }

  create(cx) {
    if (this.busy || this.view !== "new" || this.tab !== "new") return;
    const where = this.whereNow();
    if (where) return this.startRemote(where, cx);
    const form = { name: this.name.value().trim(), harness: this.harness, role: roleText(this.role.value()),
                   skill: this.skill.value().trim() };
    this.problem = checkName(form.name, this.data?.agents) ?? (form.harness ? null : "Choose a harness") ?? checkSkill(form.skill);
    if (this.problem) { cx.notify(); return; }
    this.busy = true;
    cx.notify();
    cx.spawn(async (cx) => {
      const r = await call(addArgs(form.name, form.harness, form.role, form.skill));
      this.busy = false;
      this.finish(settle(form, r), form.name, "ready", (problem) => { this.problem = problem; }, cx);
      cx.notify();
    });
  }

  loadAgent(row, cx) {
    if (this.busy) return;
    this.busy = true;
    this.loadProblem = null;
    cx.notify();
    cx.spawn(async (cx) => {
      const r = await call(loadArgs(row.name));
      this.busy = false;
      this.finish(settle({ name: row.name }, r), row.name, "loaded",
                  (problem) => { this.loadProblem = { name: row.name, text: problem }; }, cx);
      cx.notify();
    });
  }

  save(a, cx) {
    if (this.busy) return;
    const skill = this.editSkill.value().trim();
    this.note = checkSkill(skill);
    if (this.note) { cx.notify(); return; }
    this.busy = true;
    cx.notify();
    cx.spawn(async (cx) => {
      try {
        const out = await cockpit(exec, home, setArgs(a.name, roleText(this.editRole.value()), skill), false);
        const missing = out.split("\n").find((l) => l.startsWith("! "));
        this.note = missing ? plain(missing.slice(2), 300) : null;
        notify({ title: "Agents", body: `${a.name} saved`, level: "success" });
        this.reload(cx, a.name);
      } catch (e) {
        this.note = plain(e?.message ?? e, 300);
      } finally {
        this.busy = false;
      }
      cx.notify();
    });
  }

  // Removing ends this installation's token in moshi; the agent stays there for the other installations.
  removeText(a) {
    if (a.has_moshi_token) return `Remove ${a.name} from this Mac? This installation's token ends; moshi keeps the agent.`;
    return `Remove ${a.name} from this Mac? Only the local entry goes; moshi is not touched.`;
  }

  remove(a, cx) {
    if (this.busy) return;
    this.busy = true;
    cx.notify();
    cx.spawn(async (cx) => {
      try {
        const out = await cockpit(exec, home, removeArgs(a.name), false);
        notify({ title: "Agents", body: plain(out.replace(/• /g, ""), 400), level: "success" });
        this.confirming = false;
        this.reload(cx);
      } catch (e) {
        this.error = plain(e?.message ?? e, 200);
      } finally {
        this.busy = false;
      }
      cx.notify();
    });
  }

  status(a) {
    if (!this.data?.servers?.moshi) return "Local only";
    if (!a.native) return "No moshi";
    return a.connected ? "Connected" : "Not connected";
  }

  listRow(t, a) {
    const on = this.view === "manage" && this.selected === a.name;
    return h_flex().id(`agent-${a.name}`).w_full().min_w_0().gap(8).px(8).py(6).rounded(6).items_center()
      .bg(on ? t.selected : t.surface).hover((s) => s.bg(t.hover))
      .on_click((_e, cx) => this.pick(a.name, cx))
      .child(div().flex_shrink_0().w(7).h(7).rounded(4).bg(a.connected ? t.working : t.dim))
      .child(div().flex_1().min_w_0().text_size(13).text_color(t.foreground).truncate().child(a.name));
  }

  field(t, label, value) {
    return h_flex().w_full().gap(8)
      .child(div().w(80).flex_shrink_0().text_size(12).text_color(t.dim).child(label))
      .child(div().flex_1().min_w_0().text_size(13).text_color(t.foreground).child(value));
  }

  details(t) {
    const a = this.agent();
    if (!a) return div().text_size(13).text_color(t.dim).child("No agents yet. Add one with + Agent.");
    return v_flex().w_full().gap(10)
      .child(div().font_semibold().text_size(16).text_color(t.foreground).child(a.name))
      .child(this.field(t, "Harness", a.harness + (a.installed ? "" : " (not installed)")))
      .child(this.field(t, "Role", this.input(t, this.editRole)))
      .child(this.field(t, "Skill", this.data?.servers?.ocellio ? this.input(t, this.editSkill)
        : div().text_size(12).text_color(t.dim).child("Needs ocellio on this machine")))
      .child(this.field(t, "moshi", this.status(a)))
      .when(!!this.note, (el) => el.child(div().text_size(12).text_color(t.blocked).child(this.note)))
      .child(h_flex().gap(6).pt(6)
        .child(btn("agent-save", t, this.busy ? "Saving…" : "Save", (_e, cx) => this.save(a, cx), { enabled: !this.busy }))
        .child(btn("agent-remove", t, "Remove…", (_e, cx) => { this.confirming = !this.confirming; cx.notify(); }, { on: this.confirming })))
      .when(this.confirming, (el) => el
        .child(div().text_size(12).text_color(t.foreground).child(this.removeText(a)))
        .child(h_flex().gap(6)
          .child(btn("agent-remove-yes", t, this.busy ? "Removing…" : "Yes, remove", (_e, cx) => this.remove(a, cx), { enabled: !this.busy }))
          .child(btn("agent-remove-no", t, "Cancel", (_e, cx) => { this.confirming = false; cx.notify(); }))));
  }

  input(t, state) {
    return Input.new(state).w_full().h(32).px(8).rounded(6).border_1().border_color(t.input).bg(t.background).text_size(12);
  }

  // A harness: selectable when the CLI finds it and it is free; greyed with its reason; missing → greyed with Install.
  harnessChip(t, h) {
    if (h.selectable) {
      return btn(`agent-h-${h.name}`, t, h.label, (_e, cx) => { this.harness = h.name; this.problem = null; cx.notify(); },
        { on: this.harness === h.name });
    }
    const chip = btn(`agent-h-${h.name}`, t, h.label, null, { enabled: false });
    if (!h.install) return chip;
    return h_flex().gap(4).items_center().child(chip)
      .child(btn(`agent-install-${h.name}`, t, "Install", () => {
        run("tab.new", { name: h.install.name, argv: h.install.argv });
        closeOverlay(props().overlay);
      }));
  }

  newTab(t) {
    const choices = whereChoices(machines());
    const where = this.whereNow(choices);
    return v_flex().w_full().gap(10)
      .child(h_flex().w_full().flex_wrap().gap_x(6).gap_y(6).items_center()
        .child(div().text_size(12).text_color(t.dim).child("Where"))
        .children(choices.map((c, i) => btn(`agent-where-${i}`, t, c.label, (_e, cx) => this.pickWhere(c.name, cx),
          { on: c.name === where }))))
      .child(where ? this.remoteForm(t, where) : this.localForm(t));
  }

  // Another machine: its harnesses from the inventory, Start opens a new space there.
  remoteForm(t, machine) {
    const entry = this.remote[machine];
    const chips = remoteChips(entry?.rows, machine, entry?.known);
    const notes = chips.filter((c) => c.note);
    return v_flex().w_full().gap(10)
      .when(!entry || entry.loading, (el) => el.child(div().text_size(12).text_color(t.dim).child(`Checking ${machine}…`)))
      .when(!!entry?.error, (el) => el.child(div().text_size(12).text_color(t.blocked).child(entry.error)))
      .child(h_flex().w_full().flex_wrap().gap_x(6).gap_y(6).items_center()
        .children(chips.map((c) => btn(`agent-rh-${c.name}`, t, c.label,
          (_e, cx) => { this.remoteHarness = c.name; this.problem = null; cx.notify(); },
          { on: this.remoteHarness === c.name, enabled: c.selectable }))))
      .when(notes.length > 0, (el) => el.child(div().text_size(11).text_color(t.dim).child(notes.map((c) => c.note).join(" · "))))
      .child(h_flex().gap(8).items_center()
        .child(btn("agent-start", t, "Start", (_e, cx) => this.create(cx), { primary: true, enabled: !!entry?.rows }))
        .child(div().text_size(12).text_color(this.problem ? t.blocked : t.dim)
          .child(this.problem || `Starts in a new space on ${machine}`)));
  }

  localForm(t) {
    const rows = harnessRows(this.data?.harnesses, this.known, cli());
    const refused = rows.filter((h) => (h.installed || h.herdrOnly) && !h.selectable && h.note);
    return v_flex().w_full().gap(10)
      .child(this.input(t, this.name))
      .child(h_flex().w_full().flex_wrap().gap_x(6).gap_y(6).items_center()
        .children(rows.map((h) => this.harnessChip(t, h))))
      .when(refused.length > 0, (el) => el.child(div().text_size(11).text_color(t.dim).child(
        refused.map((h) => `${h.label}: ${plain(h.note, 160)}`).join(" · "))))
      .child(this.input(t, this.role))
      .when(!!this.data?.servers?.ocellio, (el) => el.child(this.input(t, this.skill)))
      .child(h_flex().gap(8).items_center()
        .child(btn("agent-create", t, this.busy ? "Creating…" : "Create", (_e, cx) => this.create(cx), { primary: true, enabled: !this.busy }))
        .child(div().text_size(12).text_color(this.problem ? t.blocked : t.dim).child(this.problem || "Enter creates it")));
  }

  catalogRow(t, r) {
    const problem = this.loadProblem?.name === r.name ? this.loadProblem.text : "";
    const about = [r.harness, r.role, r.where].filter(Boolean).join(" · ");
    return v_flex().id(`catalog-${r.name}`).w_full().gap(2).px(8).py(6).rounded(6).bg(t.surface).flex_none()
      .child(h_flex().w_full().gap(8).items_center()
        .child(v_flex().flex_1().min_w_0()
          .child(div().text_size(13).font_semibold().text_color(r.loadable ? t.foreground : t.dim).truncate().child(r.name))
          .child(div().text_size(11).text_color(t.dim).truncate().child(about)))
        .child(btn(`catalog-load-${r.name}`, t, this.busy ? "Loading…" : "Load", (_e, cx) => this.loadAgent(r, cx),
          { enabled: r.loadable && !this.busy })))
      .when(!!r.note, (el) => el.child(div().text_size(11).text_color(t.dim).child(r.note)))
      .when(!!problem, (el) => el.child(div().text_size(12).text_color(t.blocked).child(problem)));
  }

  moshiTab(t) {
    const rows = this.catalog ?? [];
    return v_flex().w_full().flex_1().min_h(0).gap(4).overflow_y_scrollbar()
      .when(!this.catalog && !this.catalogProblem, (el) => el.child(div().text_size(12).text_color(t.dim).child("Loading moshi's agents…")))
      .when(!!this.catalogProblem, (el) => el.child(div().text_size(12).text_color(t.blocked).child(this.catalogProblem)))
      .when(!!this.catalog && rows.length === 0, (el) => el.child(div().text_size(12).text_color(t.dim)
        .child("Every agent in moshi is on this Mac already.")))
      .children(rows.map((r) => this.catalogRow(t, r)));
  }

  // `cx`: render's, for the Machines tab (it reads Herdr's machine list again when Herdr reports a machine ready).
  addView(t, cx) {
    return v_flex().w_full().flex_1().min_h(0).gap(10)
      .child(h_flex().gap(6).items_center()
        .child(btn("agent-tab-new", t, "New", (_e, cx) => this.showTab("new", cx), { on: this.tab === "new" }))
        .child(btn("agent-tab-moshi", t, "From moshi", (_e, cx) => this.showTab("moshi", cx), { on: this.tab === "moshi" }))
        .child(btn("agent-tab-machines", t, "Machines", (_e, cx) => this.showTab("machines", cx), { on: this.tab === "machines" }))
        .when(this.tab === "machines", (el) => el.child(div().flex_1()).child(machinesLegend(t))))
      .child(this.tab === "moshi" ? this.moshiTab(t) : this.tab === "machines" ? machinesTab(this, t, cx) : this.newTab(t));
  }

  render(cx) {
    const t = theme();
    if (this.view === "connect") {
      return div().id("root").size_full().track_focus(this.handle).on_key_down(connectKey).child(connectView(t));
    }
    if (this.view === "start") {
      return div().id("root").size_full().track_focus(this.handle)
        .on_key_down((e, cx) => startKey(this, e, cx)).child(startView(this, t));
    }
    const list = this.data?.agents ?? [];
    return h_flex().id("root").size_full().bg(t.popover)
      .child(v_flex().w(190).flex_shrink_0().h_full().p(10).gap(4).border_r_1().border_color(t.border)
        .child(div().font_semibold().text_size(16).text_color(t.foreground).pb(8).child("Agents"))
        .child(v_flex().flex_1().min_h(0).gap(2).overflow_y_scrollbar().children(list.map((a) => this.listRow(t, a))))
        .child(btn("agent-new", t, "+ Agent", (_e, cx) => this.showNew(cx), { on: this.view === "new" })))
      .child(v_flex().flex_1().min_w_0().h_full().p(16).gap(10)
        .child(v_flex().flex_1().min_h(0).child(this.view === "new" ? this.addView(t, cx) : this.details(t)))
        .when(!!this.error, (el) => el.child(div().text_size(12).text_color(t.blocked).child(this.error)))
        .child(h_flex().w_full().justify_end().child(btn("agents-close", t, "Close", () => closeOverlay(props().overlay)))));
  }
}
