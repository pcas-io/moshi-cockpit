// mesh-spaces: filter, pinned spaces, groups, pruning of the stored entries and the right-click menu.
// Run: node --test tests/js/
import { test } from "node:test";
import assert from "node:assert/strict";
import { GROUPS, PINS, addToGroup, blocks, dropAction, filterRows, matches, nameRefusal, newGroup, pin, prune, removeFromGroup, sections,
         unpin, validEntries }
  from "../../share/herdr-mods/mesh-spaces/organize.js";
import { act, groupAction, menuItems, writes } from "../../share/herdr-mods/mesh-spaces/menu.js";

// A row as ordered() makes it: the space, its index, its git, its tree mark (null for a checkout).
const space = (id, extra = {}) => ({ id, name: id.split("/").pop(), folder: "", branch: null, machine: null, ...extra });
const row = (id, i, extra = {}, tree = null) => ({ w: space(id, extra), i, g: null, tree });
const ids = (rows) => rows.map((r) => r.w.id);
const shape = (secs) => secs.map((s) => [s.kind, s.id ?? null, ids(s.rows)]);

test("matches name, folder, branch and machine, ignores case and nulls", () => {
  assert.equal(PINS, "mesh-spaces.pins");
  assert.equal(GROUPS, "mesh-spaces.groups");
  const s = { name: "Api", folder: "/srv/Payments", branch: "feature/Login", machine: "devbox" };
  for (const q of ["api", "API", "payments", "login", "DEVBOX", "srv/pay"]) assert.equal(matches(s, q), true, q);
  assert.equal(matches(s, "build-1"), false);
  for (const q of ["", "   ", null, undefined]) assert.equal(matches(s, q), true, String(q));
  const bare = { name: "web", folder: null, branch: null, machine: null };
  assert.equal(matches(bare, "web"), true);
  assert.equal(matches(bare, "null"), false);
  assert.equal(matches(bare, " web "), true); // the query is trimmed
});

test("a matching worktree keeps its checkout", () => {
  const rows = [row("s1", 0), row("s2", 1, { branch: "spike" }, "├─"), row("s3", 2, {}, "└─"), row("s4", 3)];
  assert.deepEqual(ids(filterRows(rows, "spike")), ["s1", "s2"]);
  assert.deepEqual(ids(filterRows(rows, "s4")), ["s4"]);
  assert.deepEqual(ids(filterRows(rows, "s1")), ["s1"]); // a matching checkout alone
  assert.deepEqual(ids(filterRows(rows, "s")), ["s1", "s2", "s3", "s4"]); // order unchanged
  assert.equal(filterRows(rows, " "), rows);
  assert.deepEqual(filterRows(rows, "nothing"), []);
});

test("sections put pinned, then groups of this machine, then the rest", () => {
  const rows = [row("devbox/s1", 0, { machine: "devbox" }), row("devbox/s2", 1, { machine: "devbox" }),
                row("devbox/s3", 2, { machine: "devbox" }), row("devbox/s4", 3, { machine: "devbox" }),
                row("devbox/s5", 4, { machine: "devbox" })];
  const pins = [{ id: "devbox/s4", machine: "devbox" }, { id: "devbox/s2", machine: "devbox" },
                { id: "build-1/s2", machine: "build-1" }];
  const groups = [{ id: "g2", name: "Ops", machine: "devbox", spaces: ["devbox/s5", "devbox/s3"] },
                  { id: "g9", name: "Other", machine: "build-1", spaces: ["devbox/s1"] },
                  { id: "g3", name: "Empty", machine: "devbox", spaces: ["devbox/gone"] },
                  { id: "g1", name: "Web", machine: "devbox", spaces: ["devbox/s1"] }];
  assert.deepEqual(shape(sections(rows, pins, groups, "devbox")), [
    ["pinned", null, ["devbox/s2", "devbox/s4"]],
    ["group", "g2", ["devbox/s3", "devbox/s5"]],
    ["group", "g1", ["devbox/s1"]],
  ]);
  assert.equal(sections(rows, pins, groups, "devbox")[1].name, "Ops");
  // this computer: machine null; nothing pinned or grouped there, all in the rest
  const local = [row("s1", 0), row("s2", 1)];
  assert.deepEqual(shape(sections(local, pins, groups, null)), [["rest", null, ["s1", "s2"]]]);
  assert.deepEqual(shape(sections(local, [{ id: "s2", machine: null }], [], null)),
                   [["pinned", null, ["s2"]], ["rest", null, ["s1"]]]);
  assert.deepEqual(sections([], pins, groups, "devbox"), []);
});

test("worktrees follow their checkout's section", () => {
  const rows = [row("s1", 0), row("s2", 1, {}, "├─"), row("s3", 2, {}, "└─"), row("s4", 3), row("s5", 4, {}, "└─")];
  assert.deepEqual(blocks(rows).map((b) => [b.root.w.id, ids(b.rows)]), [["s1", ["s1", "s2", "s3"]], ["s4", ["s4", "s5"]]]);
  const pins = [{ id: "s4", machine: null }, { id: "s2", machine: null }]; // a pinned worktree goes with its checkout
  const groups = [{ id: "g1", name: "Web", machine: null, spaces: ["s1"] }];
  assert.deepEqual(shape(sections(rows, pins, groups, null)),
                   [["pinned", null, ["s4", "s5"]], ["group", "g1", ["s1", "s2", "s3"]]]);
});

test("pin and group exclude each other", () => {
  const groups = [{ id: "g1", name: "Web", machine: "devbox", spaces: ["devbox/s1", "devbox/s2"] },
                  { id: "g2", name: "Ops", machine: "devbox", spaces: ["devbox/s3"] }];
  let st = pin([], groups, "devbox/s1", "devbox");
  assert.deepEqual(st.pins, [{ id: "devbox/s1", machine: "devbox" }]);
  assert.deepEqual(st.groups[0].spaces, ["devbox/s2"]);
  assert.deepEqual(pin(st.pins, st.groups, "devbox/s1", "devbox").pins, st.pins); // pinned once
  st = addToGroup(st.pins, st.groups, "g2", "devbox/s1");
  assert.deepEqual(st.pins, []);
  assert.deepEqual(st.groups.map((g) => g.spaces), [["devbox/s2"], ["devbox/s3", "devbox/s1"]]);
  st = addToGroup(st.pins, st.groups, "g1", "devbox/s1"); // moves out of the other group
  assert.deepEqual(st.groups.map((g) => g.spaces), [["devbox/s2", "devbox/s1"], ["devbox/s3"]]);
  assert.deepEqual(addToGroup(st.pins, st.groups, "g1", "devbox/s1").groups, st.groups); // already there
  assert.deepEqual(addToGroup(st.pins, st.groups, "nope", "devbox/s1"), st); // unknown group: unchanged
  assert.deepEqual(unpin([{ id: "s1", machine: null }, { id: "s2", machine: null }], "s1"), [{ id: "s2", machine: null }]);
  assert.deepEqual(groups[0].spaces, ["devbox/s1", "devbox/s2"]); // inputs never change
});

test("new group names are cleaned and unique per machine", () => {
  const groups = [{ id: "g1", name: "Web", machine: "devbox", spaces: ["devbox/s1"] }];
  const made = newGroup(groups, "devbox", "  Ops\u0007 team\n ", "devbox/s2", "g2");
  assert.deepEqual(made[1], { id: "g2", name: "Ops team", machine: "devbox", spaces: ["devbox/s2"] });
  assert.equal(made[0], groups[0]);
  for (const bad of ["", "   ", "\u0001\u0002", "x".repeat(41), "web", " WEB "]) {
    assert.equal(newGroup(groups, "devbox", bad, "devbox/s2", "g2"), groups, JSON.stringify(bad));
  }
  assert.equal(newGroup(groups, "devbox", "x".repeat(40), "devbox/s2", "g2")[1].name, "x".repeat(40));
  // the same name on another machine is fine
  assert.equal(newGroup(groups, "build-1", "Web", "build-1/s2", "g2")[1].machine, "build-1");
  assert.equal(newGroup(groups, null, "web", "s2", "g2")[1].machine, null);
  // the space leaves the group it was in (and that group goes when left empty)
  assert.deepEqual(newGroup(groups, "devbox", "Ops", "devbox/s1", "g2").map((g) => g.id), ["g2"]);
});

test("removing the last member removes the group", () => {
  const groups = [{ id: "g1", name: "Web", machine: null, spaces: ["s1", "s2"] },
                  { id: "g2", name: "Ops", machine: null, spaces: ["s3"] }];
  assert.deepEqual(removeFromGroup(groups, "s1").map((g) => [g.id, g.spaces]), [["g1", ["s2"]], ["g2", ["s3"]]]);
  assert.deepEqual(removeFromGroup(groups, "s3").map((g) => g.id), ["g1"]);
  assert.equal(removeFromGroup(groups, "s9"), groups);
  assert.deepEqual(pin([], groups, "s3", null).groups.map((g) => g.id), ["g1"]);
});

test("prune keeps entries of machines whose layout is not ready", () => {
  const pins = [{ id: "devbox/s1", machine: "devbox" }, { id: "build-1/s2", machine: "build-1" }];
  const groups = [{ id: "g1", name: "Web", machine: "devbox", spaces: ["devbox/s3"] }];
  // devbox is offline: its spaces vanished from spaces(), its pins and groups stay
  const kept = prune(pins, groups, new Set(["build-1/s2"]), new Set([null, "build-1"]));
  assert.equal(kept.pins, pins);
  assert.equal(kept.groups, groups);
  // nothing ready at all (still loading)
  assert.equal(prune(pins, groups, new Set(), new Set()).pins, pins);
});

test("prune drops vanished spaces of ready machines", () => {
  const pins = [{ id: "devbox/s1", machine: "devbox" }, { id: "s1", machine: null }, { id: "build-1/s2", machine: "build-1" }];
  const groups = [{ id: "g1", name: "Web", machine: "devbox", spaces: ["devbox/s3", "devbox/s4"] },
                  { id: "g2", name: "Ops", machine: null, spaces: ["s5"] }];
  const kept = prune(pins, groups, new Set(["devbox/s4", "build-1/s2"]), new Set([null, "devbox"]));
  assert.deepEqual(kept.pins, [{ id: "build-1/s2", machine: "build-1" }]);
  assert.deepEqual(kept.groups, [{ id: "g1", name: "Web", machine: "devbox", spaces: ["devbox/s4"] }]);
  // all live: the same arrays back, so the caller writes nothing
  const live = new Set(["devbox/s1", "s1", "build-1/s2", "devbox/s3", "devbox/s4", "s5"]);
  const same = prune(pins, groups, live, new Set([null, "devbox", "build-1"]));
  assert.equal(same.pins, pins);
  assert.equal(same.groups, groups);
});

test("damaged stored entries are left out", () => {
  const pins = [{ id: "s1", machine: null }, { machine: null }, null, { id: 7 }];
  assert.deepEqual(validEntries(PINS, pins), [{ id: "s1", machine: null }]);
  const groups = [{ id: "g1", name: "Web", machine: null, spaces: ["s1"] }, { id: "g2", name: 5, machine: null, spaces: [] },
                  { id: "g3", machine: null, spaces: ["s2"] }, { id: "g4", name: "Ops", machine: null }];
  assert.deepEqual(validEntries(GROUPS, groups).map((g) => g.id), ["g1"]);
  for (const bad of [undefined, null, "x", { id: "s1" }]) assert.deepEqual(validEntries(GROUPS, bad), []);
  const clean = [{ id: "g1", name: "Web", machine: null, spaces: [] }];
  assert.equal(validEntries(GROUPS, clean), clean); // nothing damaged: the same array back
});

// A drag: the dragged space and where it is dropped (a row in its section, or a group's header).
const drag = (id, machine = "devbox", nested = false) => ({ id, machine, nested });
const onRow = (id, section, index) => ({ kind: "row", id, section, index });
const onHead = (id) => ({ kind: "group", id });
const PINNED = { kind: "pinned" }, REST = { kind: "rest" };
const inGroup = (id) => ({ kind: "group", id });

test("a space dropped into another section joins it and lands where it was dropped", () => {
  const pins = [{ id: "devbox/s1", machine: "devbox" }];
  const groups = [{ id: "g1", name: "Web", machine: "devbox", spaces: ["devbox/s2", "devbox/s3"] },
                  { id: "g2", name: "Ops", machine: "devbox", spaces: ["devbox/s4"] }];
  // rest → a group's row: into that group, moved to the drop row's index
  assert.deepEqual(dropAction(drag("devbox/s5"), onRow("devbox/s3", inGroup("g1"), 3), pins, groups, "devbox"),
                   { pins, groups: [{ ...groups[0], spaces: ["devbox/s2", "devbox/s3", "devbox/s5"] }, groups[1]], move: 3 });
  // a pinned space into a group: unpinned
  const pinnedToGroup = dropAction(drag("devbox/s1"), onRow("devbox/s4", inGroup("g2"), 4), pins, groups, "devbox");
  assert.deepEqual(pinnedToGroup, { pins: [], groups: [groups[0], { ...groups[1], spaces: ["devbox/s4", "devbox/s1"] }], move: 4 });
  // rest → a pinned row: pinned
  assert.deepEqual(dropAction(drag("devbox/s5"), onRow("devbox/s1", PINNED, 1), pins, groups, "devbox"),
                   { pins: [...pins, { id: "devbox/s5", machine: "devbox" }], groups, move: 1 });
  // a grouped space onto a pinned row: pinned, out of its group (the group it leaves empty goes)
  assert.deepEqual(dropAction(drag("devbox/s4"), onRow("devbox/s1", PINNED, 1), pins, groups, "devbox"),
                   { pins: [...pins, { id: "devbox/s4", machine: "devbox" }], groups: [groups[0]], move: 1 });
  // pinned → the rest: unpinned, groups untouched
  const pinnedToRest = dropAction(drag("devbox/s1"), onRow("devbox/s5", REST, 5), pins, groups, "devbox");
  assert.deepEqual(pinnedToRest, { pins: [], groups, move: 5 });
  assert.equal(pinnedToRest.groups, groups); // unchanged list: the very array, so nothing is written for it
  // a group → the rest: out of its group
  assert.deepEqual(dropAction(drag("devbox/s2"), onRow("devbox/s5", REST, 5), pins, groups, "devbox"),
                   { pins, groups: [{ ...groups[0], spaces: ["devbox/s3"] }, groups[1]], move: 5 });
  // a group → another group's row: moved over (the group it leaves empty goes)
  assert.deepEqual(dropAction(drag("devbox/s4"), onRow("devbox/s2", inGroup("g1"), 2), pins, groups, "devbox"),
                   { pins, groups: [{ ...groups[0], spaces: ["devbox/s2", "devbox/s3", "devbox/s4"] }], move: 2 });
  // no index for the drop row: the section still changes, nothing is moved
  assert.equal(dropAction(drag("devbox/s5"), onRow("devbox/s1", PINNED, null), pins, groups, "devbox").move, null);
  assert.deepEqual(groups[0].spaces, ["devbox/s2", "devbox/s3"]); // inputs never change
  assert.deepEqual(pins, [{ id: "devbox/s1", machine: "devbox" }]);
});

test("a space dropped on a group's header joins that group without moving", () => {
  const pins = [{ id: "s1", machine: null }];
  const groups = [{ id: "g1", name: "Web", machine: null, spaces: ["s2"] }, { id: "g2", name: "Ops", machine: null, spaces: ["s3"] }];
  assert.deepEqual(dropAction(drag("s4", null), onHead("g1"), pins, groups, null),
                   { pins, groups: [{ ...groups[0], spaces: ["s2", "s4"] }, groups[1]], move: null });
  assert.deepEqual(dropAction(drag("s1", null), onHead("g2"), pins, groups, null),
                   { pins: [], groups: [groups[0], { ...groups[1], spaces: ["s3", "s1"] }], move: null });
  assert.deepEqual(dropAction(drag("s3", null), onHead("g1"), pins, groups, null),
                   { pins, groups: [{ ...groups[0], spaces: ["s2", "s3"] }], move: null });
  // its own group's header, or a group that is gone: nothing changes
  for (const head of [onHead("g1"), onHead("gone")]) {
    const same = dropAction(drag("s2", null), head, pins, groups, null);
    assert.equal(same.pins, pins);
    assert.equal(same.groups, groups);
    assert.equal(same.move, null);
  }
});

test("a drop in its own section, or of a worktree, only moves it; another machine's does nothing", () => {
  const pins = [{ id: "devbox/s1", machine: "devbox" }, { id: "devbox/s6", machine: "devbox" }];
  const groups = [{ id: "g1", name: "Web", machine: "devbox", spaces: ["devbox/s2", "devbox/s3"] }];
  const only = (result, move) => {
    assert.equal(result.pins, pins);
    assert.equal(result.groups, groups);
    assert.equal(result.move, move);
  };
  only(dropAction(drag("devbox/s2"), onRow("devbox/s3", inGroup("g1"), 3), pins, groups, "devbox"), 3);
  only(dropAction(drag("devbox/s1"), onRow("devbox/s6", PINNED, 6), pins, groups, "devbox"), 6);
  only(dropAction(drag("devbox/s4"), onRow("devbox/s5", REST, 5), pins, groups, "devbox"), 5);
  // a worktree follows its checkout: moved in Herdr's order, never into a section; on a header nothing happens
  only(dropAction(drag("devbox/s7", "devbox", true), onRow("devbox/s2", inGroup("g1"), 2), pins, groups, "devbox"), 2);
  only(dropAction(drag("devbox/s7", "devbox", true), onRow("devbox/s1", PINNED, 1), pins, groups, "devbox"), 1);
  only(dropAction(drag("devbox/s7", "devbox", true), onHead("g1"), pins, groups, "devbox"), null);
  // a pin of the same id on another machine doesn't make it pinned here
  assert.deepEqual(dropAction(drag("s1", null), onRow("s9", REST, 9), pins, [], null), { pins, groups: [], move: 9 });
  // spaces move only within their machine
  assert.equal(dropAction(drag("build-1/s1", "build-1"), onRow("devbox/s3", inGroup("g1"), 3), pins, groups, "devbox"), null);
  assert.equal(dropAction(drag("s1", null), onHead("g1"), pins, groups, "devbox"), null);
});

test("a drop writes only the lists that changed", () => {
  const pins = [{ id: "s1", machine: null }];
  const groups = [{ id: "g1", name: "Web", machine: null, spaces: ["s2"] }];
  assert.deepEqual(writes(pins, groups, dropAction(drag("s1", null), onRow("s3", REST, 3), pins, groups, null)).writes, [[PINS, []]]);
  assert.deepEqual(writes(pins, groups, dropAction(drag("s3", null), onHead("g1"), pins, groups, null)).writes,
                   [[GROUPS, [{ ...groups[0], spaces: ["s2", "s3"] }]]]);
  assert.deepEqual(writes(pins, groups, dropAction(drag("s3", null), onRow("s4", REST, 4), pins, groups, null)).writes, []);
});

// What a menu item does, recorded instead of done: the host calls menu.js makes.
function fakeHost() {
  const calls = [];
  return { calls, setShared: (k, v) => calls.push(["setShared", k, v]), close: () => calls.push(["close"]),
           openOverlay: (o) => calls.push(["openOverlay", o]), ask: () => calls.push(["ask"]) };
}
const labels = (items) => items.map((i) => i.label);
const item = (items, key) => items.find((i) => i.key === key);

test("menu offers pin, groups of this machine and More", () => {
  const groups = [{ id: "g1", name: "Web", machine: "devbox", spaces: ["devbox/s1"] },
                  { id: "g2", name: "Ops", machine: "devbox", spaces: ["devbox/s3"] },
                  { id: "g9", name: "Far", machine: "build-1", spaces: ["build-1/s2"] }];
  const grouped = menuItems("devbox/s1", "devbox", false, [], groups);
  assert.deepEqual(labels(grouped), ["Pin", "Move to Ops", "New group…", "Remove from Web", "", "More…"]);
  assert.deepEqual(grouped.map((i) => i.key), ["pin", "move-g2", "new-group", "remove", "divider", "more"]);
  // pinning takes it out of its group (the group it leaves empty goes); both lists are written
  assert.deepEqual(item(grouped, "pin").action.writes,
                   [[PINS, [{ id: "devbox/s1", machine: "devbox" }]], [GROUPS, [groups[1], groups[2]]]]);
  assert.deepEqual(item(grouped, "move-g2").action.writes,
                   [[GROUPS, [{ ...groups[1], spaces: ["devbox/s3", "devbox/s1"] }, groups[2]]]]);
  assert.deepEqual(item(grouped, "remove").action.writes, [[GROUPS, [groups[1], groups[2]]]]);
  assert.deepEqual(item(grouped, "new-group").action, { ask: true });
  // pinned: Unpin, and moving into a group unpins it
  const pins = [{ id: "devbox/s4", machine: "devbox" }];
  const pinned = menuItems("devbox/s4", "devbox", false, pins, groups);
  assert.deepEqual(labels(pinned), ["Unpin", "Move to Web", "Move to Ops", "New group…", "", "More…"]);
  assert.deepEqual(item(pinned, "unpin").action.writes, [[PINS, []]]);
  assert.deepEqual(item(pinned, "move-g1").action.writes,
                   [[PINS, []], [GROUPS, [{ ...groups[0], spaces: ["devbox/s1", "devbox/s4"] }, groups[1], groups[2]]]]);
  // a new group unpins too; a refused name changes nothing and keeps the field open
  assert.deepEqual(groupAction(pins, groups, "devbox", " Infra ", "devbox/s4", "g-x").writes,
                   [[PINS, []], [GROUPS, [...groups, { id: "g-x", name: "Infra", machine: "devbox", spaces: ["devbox/s4"] }]]]);
  for (const bad of ["", " web ", "x".repeat(41)]) assert.equal(groupAction(pins, groups, "devbox", bad, "devbox/s4", "g-x"), null);
  // this computer sees only its own groups
  assert.deepEqual(labels(menuItems("s1", null, false, [], groups)), ["Pin", "New group…", "", "More…"]);
  // every change goes to the shared store, then the menu closes; New group… only asks for the name
  const host = fakeHost();
  act(item(pinned, "move-g1").action, host);
  assert.deepEqual(host.calls.map((c) => c[0] === "setShared" ? c[1] : c[0]), [PINS, GROUPS, "close"]);
  const asking = fakeHost();
  act(item(grouped, "new-group").action, asking);
  assert.deepEqual(asking.calls, [["ask"]]);
  assert.deepEqual(groups[0].spaces, ["devbox/s1"]); // inputs never change
});

test("menu on a worktree only explains and offers More", () => {
  const groups = [{ id: "g1", name: "Web", machine: null, spaces: ["s1"] }];
  const items = menuItems("s2", null, true, [{ id: "s2", machine: null }], groups);
  assert.deepEqual(items.map((i) => [i.key, i.label]), [["note", "Worktrees follow their checkout"], ["more", "More…"]]);
  assert.equal(items[0].action, null);
  const host = fakeHost();
  act(items[0].action, host); // the note does nothing
  assert.deepEqual(host.calls, []);
});

test("More opens Herdr's own space menu", () => {
  const spec = { mod: "menu", kind: "popup", width: 200, height: 260, props: { tab: "space", target: { id: "devbox/s1" } } };
  for (const nested of [false, true]) {
    const more = item(menuItems("devbox/s1", "devbox", nested, [], []), "more");
    assert.deepEqual(more.action, { open: spec });
    const host = fakeHost();
    act(more.action, host);
    assert.deepEqual(host.calls, [["close"], ["openOverlay", spec]]); // closes ours first, writes nothing
  }
});

test("a refused group name says why", () => {
  const groups = [{ id: "g1", name: "Web", machine: "devbox", spaces: ["devbox/s1"] },
                  { id: "g2", name: "Ops", machine: null, spaces: ["s3"] }];
  for (const taken of ["Web", " web ", "WEB\u0007"]) {
    assert.equal(groupAction([], groups, "devbox", taken, "devbox/s2", "g-x"), null);
    assert.equal(nameRefusal(groups, "devbox", taken), "Taken on this machine");
  }
  for (const size of ["", "  ", "x".repeat(41)]) {
    assert.equal(groupAction([], groups, "devbox", size, "devbox/s2", "g-x"), null);
    assert.equal(nameRefusal(groups, "devbox", size), "1–40 characters");
  }
  // a name taken only on another machine is free here
  assert.notEqual(groupAction([], groups, "devbox", "Ops", "devbox/s2", "g-x"), null);
});

test("the full list has no filler row per empty machine, and the filter field stays", async () => {
  // main.js imports the host, so the view is checked in its source
  const { readFileSync } = await import("node:fs");
  const source = readFileSync(new URL("../../share/herdr-mods/mesh-spaces/main.js", import.meta.url), "utf8");
  assert.doesNotMatch(source, /\.child\("New space there"\)/);
  assert.match(source, /placeholder: "Filter spaces"/);
  assert.match(source, /e\.key !== "escape"/);
  // the machine header's "+" still makes a space there
  assert.match(source, /machine-add-\$\{m \?\? "local"\}`\)[\s\S]{0,200}run\("space\.new", \{ machine: m \?\? "" \}\)/);
});

test("rows and group headers take drops through dropAction", async () => {
  const { readFileSync } = await import("node:fs");
  const source = readFileSync(new URL("../../share/herdr-mods/mesh-spaces/main.js", import.meta.url), "utf8");
  // a row drop names the section the row is drawn in; a header drop adds to its group, nothing else is a drop zone
  assert.match(source, /this\.land\(this\.dropOn\(\{ kind: "row", id: w\.id, section, index:/);
  assert.match(source, /groupHead\(s, m, folded, filtering, t\) \{[\s\S]{0,1500}on_mouse_up\("left"[\s\S]{0,120}this\.land\(this\.dropOn\(target, m\)\)/);
  assert.equal((source.match(/this\.land\(/g) ?? []).length, 2);
  assert.ok(source.split("\n").length <= 700);
});
