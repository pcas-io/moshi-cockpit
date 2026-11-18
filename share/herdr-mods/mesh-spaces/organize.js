// What mesh-spaces adds to Herdr's list, without host imports (node --test loads it): the filter, pinned spaces
// and named groups per machine, and their upkeep. Rows are ordered() entries {w, i, g, tree}: w a space, tree
// null for a checkout and a mark for a worktree listed under it. Pins and groups live in Herdr's shared() store
// under PINS and GROUPS; `machine` is null for this computer. Every function returns new arrays and never
// changes its inputs; when nothing changes it returns the very arrays it was given.

export const PINS = "mesh-spaces.pins"; // [{id, machine}]
export const GROUPS = "mesh-spaces.groups"; // [{id, name, machine, spaces: string[]}]
const NAME_MAX = 40;

const on = (a, b) => (a ?? null) === (b ?? null);

// A list read back from the shared store (`key` PINS or GROUPS): [] when it isn't a list, entries without a string
// id (groups: also without a string name or a spaces list) left out, so a damaged value never breaks the list.
export function validEntries(key, value) {
  if (!Array.isArray(value)) return [];
  const ok = (x) => typeof x?.id === "string" && (key !== GROUPS || (typeof x.name === "string" && Array.isArray(x.spaces)));
  return value.every(ok) ? value : value.filter(ok);
}

// Case-insensitive substring over the space's name, folder, branch and machine; a blank query matches all.
export function matches(space, query) {
  const q = String(query ?? "").trim().toLowerCase();
  if (!q) return true;
  return [space.name, space.folder, space.branch, space.machine]
    .some((v) => v != null && String(v).toLowerCase().includes(q));
}

// A checkout row with the worktree rows listed under it.
export function blocks(rows) {
  const out = [];
  for (const r of rows) {
    if (r.tree && out.length) out[out.length - 1].rows.push(r);
    else out.push({ root: r, rows: [r] });
  }
  return out;
}

// The rows that match, in their order; a matching worktree brings its checkout along.
export function filterRows(rows, query) {
  if (!String(query ?? "").trim()) return rows;
  return blocks(rows).flatMap(({ root, rows: block }) => {
    const trees = block.slice(1).filter((r) => matches(r.w, query));
    return trees.length || matches(root.w, query) ? [root, ...trees] : [];
  });
}

// One machine's rows as sections: its pinned spaces, then each of its groups in their stored order (empty ones
// left out), then the rest. A worktree goes where its checkout goes; inside a section rows keep Herdr's order.
export function sections(rows, pins, groups, machine) {
  const pinned = new Set(pins.filter((p) => on(p.machine, machine)).map((p) => p.id));
  const mine = groups.filter((g) => on(g.machine, machine));
  const PINNED = {}, REST = {}; // keys no group id can be
  const home = (id) => (pinned.has(id) ? PINNED : mine.find((g) => (g.spaces ?? []).includes(id))?.id ?? REST);
  const by = new Map();
  for (const b of blocks(rows)) {
    const key = home(b.root.w.id);
    by.set(key, [...(by.get(key) ?? []), ...b.rows]);
  }
  const out = [];
  if (by.has(PINNED)) out.push({ kind: "pinned", rows: by.get(PINNED) });
  for (const g of mine) if (by.has(g.id)) out.push({ kind: "group", id: g.id, name: g.name, rows: by.get(g.id) });
  if (by.has(REST)) out.push({ kind: "rest", rows: by.get(REST) });
  return out;
}

// The space out of every group; a group it leaves empty goes.
export function removeFromGroup(groups, id) {
  if (!groups.some((g) => g.spaces.includes(id))) return groups;
  return groups.flatMap((g) => {
    if (!g.spaces.includes(id)) return [g];
    const spaces = g.spaces.filter((s) => s !== id);
    return spaces.length ? [{ ...g, spaces }] : [];
  });
}

export function unpin(pins, id) {
  return pins.some((p) => p.id === id) ? pins.filter((p) => p.id !== id) : pins;
}

// Pinned means in no group.
export function pin(pins, groups, id, machine) {
  return {
    pins: pins.some((p) => p.id === id) ? pins : [...pins, { id, machine: machine ?? null }],
    groups: removeFromGroup(groups, id),
  };
}

// Into one group: unpinned and out of any other. An unknown group changes nothing.
export function addToGroup(pins, groups, groupId, id) {
  const target = groups.find((g) => g.id === groupId);
  if (!target) return { pins, groups };
  if (target.spaces.includes(id)) return { pins: unpin(pins, id), groups };
  // the target doesn't hold it, so removeFromGroup keeps it
  const moved = removeFromGroup(groups, id).map((g) => (g === target ? { ...g, spaces: [...g.spaces, id] } : g));
  return { pins: unpin(pins, id), groups: moved };
}

// A new group with this one space in it, the space out of any other group. The name loses control characters
// and is trimmed; it has 1 to 40 characters and is unique on its machine, ignoring case. Invalid: unchanged.
export function newGroup(groups, machine, name, id, newId) {
  const clean = String(name ?? "").replace(/[\u0000-\u001f\u007f-\u009f]+/g, " ").replace(/\s+/g, " ").trim();
  const size = [...clean].length;
  const taken = groups.some((g) => on(g.machine, machine) && g.name.toLowerCase() === clean.toLowerCase());
  if (size < 1 || size > NAME_MAX || taken) return groups;
  return [...removeFromGroup(groups, id), { id: newId, name: clean, machine: machine ?? null, spaces: [id] }];
}

// Entries of spaces that are gone, dropped only for machines whose layout is ready: an offline machine's
// spaces vanish from spaces() for a while, its pins and groups wait for it.
export function prune(pins, groups, liveIds, readyMachines) {
  const gone = (id, machine) => readyMachines.has(machine ?? null) && !liveIds.has(id);
  const keptPins = pins.filter((p) => !gone(p.id, p.machine));
  let changed = false;
  const keptGroups = groups.flatMap((g) => {
    const spaces = g.spaces.filter((id) => !gone(id, g.machine));
    if (spaces.length === g.spaces.length) return [g];
    changed = true;
    return spaces.length ? [{ ...g, spaces }] : [];
  });
  return { pins: keptPins.length === pins.length ? pins : keptPins, groups: changed ? keptGroups : groups };
}
