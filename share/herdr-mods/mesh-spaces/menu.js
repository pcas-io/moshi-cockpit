// The right-click menu of a space row: Pin / Unpin, its machine's groups, New group…, and More… for Herdr's own
// space menu. No host imports, so node --test loads it: main.js opens the popup (props view "menu") and hands the
// host calls and builders in as `panel.kit` (menuInit). An action is data: {writes: [[key, list]]} for the shared
// store, {ask: true} for the group name field, {open: overlay} for More…, null for the note.

import { GROUPS, PINS, addToGroup, newGroup, pin, removeFromGroup, unpin, validEntries } from "./organize.js";

const on = (a, b) => (a ?? null) === (b ?? null);

// The shared-store writes that turn pins and groups into `next`: only the lists that changed.
function writes(pins, groups, next) {
  const out = [];
  if (next.pins !== pins) out.push([PINS, next.pins]);
  if (next.groups !== groups) out.push([GROUPS, next.groups]);
  return { writes: out };
}

// Herdr's own space menu, opened exactly as its spaces mod does.
const herdrMenu = (id) => ({ mod: "menu", kind: "popup", width: 200, height: 260, props: { tab: "space", target: { id } } });

// The menu of space `id` on `machine` (null: this computer); `nested`: a worktree listed under its checkout,
// which goes where its checkout goes, so it gets no pin or group items.
export function menuItems(id, machine, nested, pins, groups) {
  const more = { key: "more", label: "More…", action: { open: herdrMenu(id) } };
  if (nested) return [{ key: "note", label: "Worktrees follow their checkout", action: null }, more];
  const mine = groups.filter((g) => on(g.machine, machine));
  const home = mine.find((g) => g.spaces.includes(id));
  const items = [pins.some((p) => p.id === id)
    ? { key: "unpin", label: "Unpin", action: writes(pins, groups, { pins: unpin(pins, id), groups }) }
    : { key: "pin", label: "Pin", action: writes(pins, groups, pin(pins, groups, id, machine)) }];
  for (const g of mine) {
    if (g !== home) items.push({ key: `move-${g.id}`, label: `Move to ${g.name}`, action: writes(pins, groups, addToGroup(pins, groups, g.id, id)) });
  }
  items.push({ key: "new-group", label: "New group…", action: { ask: true } });
  if (home) items.push({ key: "remove", label: `Remove from ${home.name}`, action: writes(pins, groups, { pins, groups: removeFromGroup(groups, id) }) });
  items.push({ key: "divider", label: "", action: null }, more);
  return items;
}

// New group… after Enter: the group with this space in it, the space unpinned. null when newGroup refuses the name.
export function groupAction(pins, groups, machine, name, id, newId) {
  const grown = newGroup(groups, machine, name, id, newId);
  return grown === groups ? null : writes(pins, groups, addToGroup(pins, grown, newId, id));
}

// Does an item's action through `host` {setShared, close, openOverlay, ask}: store writes, then the menu closes.
export function act(action, host) {
  if (!action) return;
  if (action.ask) return host.ask();
  for (const [key, value] of action.writes ?? []) host.setShared(key, value);
  host.close();
  if (action.open) host.openOverlay(action.open);
}

const stored = (kit, key) => validEntries(key, kit.shared(key));

// The popup's state, from init. `kit`: {div, h_flex, v_flex, Input, InputState, shared, setShared, openOverlay,
// closeOverlay, props}.
export function menuInit(panel, kit) {
  const p = kit.props() ?? {};
  Object.assign(panel, { view: "menu", kit, target: p.target?.id, machine: p.machine ?? null, nested: Boolean(p.nested), asking: false });
  panel.groupName = kit.InputState.new({ placeholder: "Group name" });
  panel.groupName.on("submit", () => {
    const action = groupAction(stored(kit, PINS), stored(kit, GROUPS), panel.machine, panel.groupName.value(),
                               panel.target, `g-${Date.now().toString(36)}`);
    act(action, host(panel));
  });
}

function host(panel) {
  const { kit } = panel;
  return {
    setShared: kit.setShared,
    close: () => kit.closeOverlay(kit.props().overlay),
    openOverlay: kit.openOverlay,
    ask: () => { panel.asking = true; panel.groupName.focus(); },
  };
}

export function menuView(panel, t) {
  const { div, h_flex, v_flex, Input } = panel.kit;
  const items = menuItems(panel.target, panel.machine, panel.nested, stored(panel.kit, PINS), stored(panel.kit, GROUPS));
  const row = ({ key, label, action }) => {
    if (key === "divider") return div().h(1).mx(4).my(5).bg(t.border).flex_none();
    if (key === "note") return div().px(9).py(6).text_size(11.5).text_color(t.dim).child(label);
    if (key === "new-group" && panel.asking) {
      return div().px(4).py(2).flex_none()
        .child(Input.new(panel.groupName).w_full().h(28).px(8).rounded(6).border_1().border_color(t.input).bg(t.background));
    }
    return h_flex().id(`space-menu-${key}`).h(31).px(9).rounded(7).items_center().flex_none()
      .text_color(t.foreground).hover((s) => s.bg(t.selected))
      .on_click((_e, cx) => { act(action, host(panel)); if (action?.ask) cx.notify(); })
      .child(div().flex_1().min_w(0).truncate().child(label));
  };
  return v_flex().id("space-menu").w_full().bg(t.popover).p(5).text_size(12.8).children(items.map(row));
}
