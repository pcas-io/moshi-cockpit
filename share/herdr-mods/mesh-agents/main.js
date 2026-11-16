// "Manage agents…" and "New agent…" from the "+" menu: one dialog in the style of herdr's settings, the agents
// on the left, the selected one or the new-agent form on the right. Everything goes through `moshi-cockpit`:
// this mod never sees a token. Connecting asks for tokens, so it runs in a terminal tab.

import { View, div } from "gpui-kit";
import { v_flex, h_flex, InputState, Input } from "gpui-base";
import { exec, home, theme, run, props, closeOverlay, openOverlay, notify } from "herdr";
import { btn, cockpit, plain } from "./util.js";
import { addArgs, checkName, checkSkill, removeArgs, roleText, setArgs } from "./rules.js";
import { loadSessions, startKey, startView } from "./start.js";
import { connectKey, connectView } from "./connect.js";

const connectArgv = (name) => [`${home()}/.local/bin/moshi-cockpit`, "bot", "add", name];

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
    this.harness = null;
    this.problem = null;
    this.note = null;
    this.name = InputState.new({ placeholder: "Name, e.g. research" });
    this.name.on("submit", (_e, cx) => this.create(cx));
    this.role = InputState.new({ placeholder: "Role and personality (optional)" });
    this.role.on("submit", (_e, cx) => this.create(cx));
    this.skill = InputState.new({ placeholder: "ocellio skill (optional), e.g. code-review" });
    this.skill.on("submit", (_e, cx) => this.create(cx));
    this.editRole = InputState.new({ placeholder: "Role and personality" });
    this.editSkill = InputState.new({ placeholder: "ocellio skill (optional)" });
    if (this.view === "new") this.name.focus();
    this.load(cx, props()?.agent);
  }

  // From the session list to this agent's details: the dialog of "Manage agents…".
  manageAgent() {
    closeOverlay(props().overlay);
    openOverlay({ mod: "mesh-agents", kind: "dialog", width: 640, height: 460, props: { view: "manage", agent: this.agent } });
  }

  load(cx, select) {
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

  showNew(cx) {
    this.view = "new";
    this.confirming = false;
    this.note = null;
    this.problem = null;
    this.harness = null;
    this.name.set_value("");
    this.role.set_value("");
    this.skill.set_value("");
    this.name.focus();
    cx.notify();
  }

  create(cx) {
    if (this.busy || this.view !== "new") return;
    const name = this.name.value().trim();
    const skill = this.skill.value().trim();
    this.problem = checkName(name, this.data?.agents) ?? (this.harness ? null : "Choose a harness") ?? checkSkill(skill);
    if (this.problem) { cx.notify(); return; }
    this.busy = true;
    cx.notify();
    cx.spawn(async (cx) => {
      try {
        // One step: created and connected. Only what really cannot be done alone is left over.
        const out = await cockpit(exec, home, addArgs(name, this.harness, roleText(this.role.value()), true, skill), false);
        const missing = out.split("\n").find((l) => l.startsWith("! "));
        this.note = missing ? plain(missing.slice(2), 300) : null;
        notify({ title: "Agents", body: missing ? `${name} created, not connected yet` : `${name} ready`,
                 level: missing ? "warning" : "success" });
        this.view = "manage";
        this.load(cx, name);
      } catch (e) {
        this.problem = plain(e?.message ?? e, 200);
      } finally {
        this.busy = false;
      }
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
        this.load(cx, a.name);
      } catch (e) {
        this.note = plain(e?.message ?? e, 300);
      } finally {
        this.busy = false;
      }
      cx.notify();
    });
  }

  // Tries it without any question first; a terminal only when something has to be typed in.
  connect(a, cx) {
    if (this.busy) return;
    this.busy = true;
    this.note = null;
    cx.notify();
    cx.spawn(async (cx) => {
      try {
        await cockpit(exec, home, ["bot", "add", a.name, "--auto"], false);
        notify({ title: "Agents", body: `${a.name} connected`, level: "success" });
        this.load(cx, a.name);
      } catch (e) {
        this.note = plain(e?.message ?? e, 300);
        run("tab.new", { name: `${a.name} · connect`, argv: connectArgv(a.name) });
        closeOverlay(props().overlay);
      } finally {
        this.busy = false;
      }
      cx.notify();
    });
  }

  // Revoking needs admin access and this agent's own moshi token (never someone else's name). Not `connected`:
  // that also asks for ocellio, and the moshi token goes either way.
  revokes(a) {
    return !!(this.data?.admin && a.has_moshi_token);
  }

  removeText(a) {
    if (this.revokes(a)) return `Remove ${a.name}? Its moshi identity is revoked (it stays in the audit) and its keychain entries are deleted.`;
    if (a.has_moshi_token) {
      return `Remove ${a.name}? Its keychain entries are deleted. Its moshi identity stays active (no admin access here): revoke it on the moshi dashboard.`;
    }
    return `Remove ${a.name}? Only the local entry goes; moshi is not touched.`;
  }

  remove(a, cx) {
    if (this.busy) return;
    this.busy = true;
    cx.notify();
    cx.spawn(async (cx) => {
      try {
        const out = await cockpit(exec, home, removeArgs(a.name, this.revokes(a)), false);
        notify({ title: "Agents", body: plain(out.replace(/• /g, ""), 400), level: "success" });
        this.confirming = false;
        this.load(cx);
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
    return a.connected ? `Connected as ${a.moshi_name}` : "Not connected";
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
    if (!a) return div().text_size(13).text_color(t.dim).child("No agents yet. Create one with New agent.");
    const canConnect = !!this.data?.servers?.moshi && a.native && !a.connected;
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
        .when(canConnect, (el) => el.child(btn("agent-connect", t, this.busy ? "Connecting…" : "Connect", (_e, cx) => this.connect(a, cx), { primary: true, enabled: !this.busy })))
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

  form(t) {
    const offer = this.data?.harnesses ?? [];
    const blocked = offer.filter((h) => !h.available && h.reason);
    return v_flex().w_full().gap(10)
      .child(div().font_semibold().text_size(16).text_color(t.foreground).child("New agent"))
      .child(this.input(t, this.name))
      .child(h_flex().w_full().flex_wrap().gap_x(6).gap_y(6).items_center()
        .children(offer.filter((h) => h.available).map((h) =>
          btn(`agent-h-${h.name}`, t, h.name, (_e, cx) => { this.harness = h.name; this.problem = null; cx.notify(); },
            { on: this.harness === h.name }))))
      .when(blocked.length > 0, (el) => el.child(div().text_size(11).text_color(t.dim).child(
        blocked.map((h) => `${h.name}: ${h.reason}`).join(" · "))))
      .child(this.input(t, this.role))
      .when(!!this.data?.servers?.ocellio, (el) => el.child(this.input(t, this.skill)))
      .child(h_flex().gap(8).items_center()
        .child(btn("agent-create", t, this.busy ? "Creating…" : "Create", (_e, cx) => this.create(cx), { primary: true, enabled: !this.busy }))
        .child(div().text_size(12).text_color(this.problem ? t.blocked : t.dim).child(this.problem || "Enter creates it")));
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
        .child(btn("agent-new", t, "+ New agent", (_e, cx) => this.showNew(cx), { on: this.view === "new" })))
      .child(v_flex().flex_1().min_w_0().h_full().p(16).gap(10)
        .child(v_flex().flex_1().min_h(0).child(this.view === "new" ? this.form(t) : this.details(t)))
        .when(!!this.error, (el) => el.child(div().text_size(12).text_color(t.blocked).child(this.error)))
        .child(h_flex().w_full().justify_end().child(btn("agents-close", t, "Close", () => closeOverlay(props().overlay)))));
  }
}
