// mesh-spaces: filter, pinned spaces, groups and pruning of the stored entries. Run: node --test tests/js/
import { test } from "node:test";
import assert from "node:assert/strict";
import { GROUPS, PINS, addToGroup, blocks, filterRows, matches, newGroup, pin, prune, removeFromGroup, sections, unpin,
         validEntries }
  from "../../share/herdr-mods/mesh-spaces/organize.js";

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
