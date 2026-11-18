# moshi-cockpit

A cockpit for coding agents inside Herdr. The sidebar of the stock layout gets a third list, "Your agents",
with each of your own agents and whether it is online in moshi. A "+ Agent" dialog creates an agent, loads one
that exists in moshi or installs a missing agent CLI, and starting an agent that runs on another computer asks
before it takes it over.

## What you see

The cockpit layout is Herdr's own `main` layout with two changes: the files and changes panel is removed, and a
block called "Your agents" (id `own-agents`, weight 1, minimum 90) sits directly below the Agents list in the
sidebar. The "+" menu is a copy of Herdr's own, with the installed agent CLIs and up to three more rows:

- **Your agents** (sidebar). One row per agent: a presence dot, the name, the harness, and under it what the
  agent is working on and when it was last seen. Order: live, stale, offline, never seen, then by name. A
  single line replaces the list when something is wrong: "Not connected to moshi", "Disconnected from moshi —
  Connect again" or "moshi not reachable". The first two are clickable and open the connect dialog.
- **"+" menu.** "Connect to moshi…" (only while this Mac is not connected), "New agent…", "Manage agents…".
- **Agents dialog** (640 by 460). Agents on the left, the selected agent's role and skill on the right with
  Save and Remove, or "+ Agent" with two tabs, **New** (name, harness, role, ocellio skill) and **From
  moshi** (agents in moshi that are not on this Mac, each with "Running on <label>" where that is so).
  Harnesses that `moshi-cockpit` does not find are greyed out with an Install button ("found by Herdr, not on
  moshi-cockpit's PATH" when only Herdr finds one); `grok` is not offered under New, it has no identity of its
  own.
- **Start dialog.** Opens for an agent: a list of earlier sessions to resume, or a new one.
- **Takeover question.** In place of the list: "Running on <label>. Take over here?" with Take over and Cancel.

## How it behaves

- Agent presence (live, stale, offline, never) comes from moshi and is polled every 30 seconds; while the Mac
  is not connected it is polled every 3 seconds so the sidebar follows right after you connect.
- Clicking an agent row opens the start dialog (a running tab of the agent is one more entry there). For an
  agent whose harness is not installed here, it focuses that agent's running tab, if there is one.
- While this Mac is not connected, the connect dialog opens once per Herdr start; after that the sidebar line
  and the "+" menu lead to it.
- Clicking the connection line, or "Connect to moshi…", opens a terminal tab running `moshi-cockpit connect`:
  the browser opens, the tab shows the same code, you compare and approve. The tab ends with "Done…" and
  closes when you press Enter.
- Every start of an own agent asks `agent where` first. Every action that needs moshi runs the CLI. Exit code 3
  (approval missing or expired, or the device key missing or revoked) reopens the connect dialog; exit code 4 (active elsewhere) shows the takeover question; a name that is taken in moshi shows
  "That name is taken in moshi" and keeps what was typed.
- Take over starts the agent here with `--take-over`; Cancel starts nothing. A start that fails in its tab
  waits for Enter, and so does `harness install`, so the reason stays readable.
- moshi not reachable at start: the agent still starts locally with one warning line.
- A background `doctor --quiet` check puts one sentence in the sidebar only if something needs fixing.

## Built from

Its mods are in `share/herdr-mods/`, each with `"place": "none"` (they open as sidebar block or overlay, not as a
fixed pane), and the layout `cockpit.json`, written by `moshi-cockpit install` from Herdr's own `main` layout.

- `moshi` ("Your agents"): the sidebar block. Runs `moshi-cockpit status --json` and `doctor --quiet`; reads
  nothing else. Opens the dialogs with `openOverlay`.
- `mesh-agents`: the agents dialog, the start dialog and the connect dialog (`props.view`: `manage`, `new`,
  `start`, `connect`). Runs `agent list|catalog|add|load|set|remove|where` and `sessions` through `exec`, and
  opens `run`, `connect` and `harness install` in a new tab; never a network or a token.
- `mesh-new-menu`: the "+" menu, derived from Herdr's own new-menu (Apache-2.0), plus the rows "Connect to
  moshi…" (only while not connected), "New agent…" and "Manage agents…". Opens `connect` in a new tab.
- Herdr functions used: `exec`, `home`, `props`, `agents`, `spaces`, `panes`, `window`, `layout`, `theme`,
  `keys`, `knownAgents`, `shared`/`setShared`, `notify`, `openOverlay`, `closeOverlay`, and `run` with
  `tab.new` (for `run`, `connect`, `harness install` and the standard agents), `agent.focus` and `space.new`.
- Programs it runs: only `~/.local/bin/moshi-cockpit`, which is the command line tool in this repository.
- Art: none. The package ships no images; the Vibe entry has no icon.
- Install: `curl -fsSL https://github.com/pcas-io/moshi-cockpit/releases/latest/download/install.sh | sh`
  copies the mods and the layout; then open Herdr and switch to the `cockpit` layout.

## Watch out

- Herdr must be installed first, from herdr.dev. The versions tested are listed in `cockpit/compat.py`; other
  versions work with a warning in `moshi-cockpit doctor`.
- macOS on Apple silicon only for now.
- Mods run with full rights and these run programs: read them before you add them. They never see a
  token; tokens live in the Keychain behind the CLI. See `docs/security.md`.
- The cockpit layout is generated from Herdr's `main` layout at install time. If a later Herdr changes `main`,
  run `moshi-cockpit install` again.
- moshi and ocellio are optional. Without moshi there is no presence and no takeover; agents start locally.
