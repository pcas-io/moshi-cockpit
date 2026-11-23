# moshi-cockpit

A cockpit for coding agents inside Herdr. The sidebar of the stock layout gets a third list, "Your agents",
with each of your own agents and whether it is online in moshi. A "+ Agent" dialog creates an agent, loads one
that exists in moshi or installs a missing agent CLI, and starting an agent that runs on another computer asks
before it takes it over. A Machines tab shows the agent CLIs on this Mac and on Herdr's machines, installs and
connects moshi-cockpit on Herdr's Linux machines so agents there get their own moshi identity, and the spaces
list gets a filter, pins and groups.

## What you see

The cockpit layout is Herdr's own `main` layout with two changes: the files and changes panel is removed, and a
block called "Your agents" (id `own-agents`, weight 1, minimum 90) sits directly below the Agents list in the
sidebar. The "+" menu is a copy of Herdr's own, with the installed agent CLIs and up to four more rows:

- **Your agents** (sidebar). One row per agent: a presence dot, the name, the harness, and under it what the
  agent is working on and when it was last seen. Order: live, stale, offline, never seen, then by name. The
  agents of Herdr's Linux machines are in the same list with their moshi presence; the row's tooltip names the
  machine ("on <machine>"), and only machines Herdr still lists show. A single line replaces the list when
  something is wrong: "Not connected to moshi", "Disconnected from moshi — Connect again" or "moshi not
  reachable". The first two are clickable and open the connect dialog.
- **"+" menu.** "Connect to moshi…" (only while this Mac is not connected), "New agent…", "Manage agents…", "Machine…".
- **Agents dialog** (640 by 460). Agents on the left, the selected agent's role and skill on the right with
  Save and Remove, or "+ Agent" with three tabs, **New** (name, harness, role, ocellio skill), **From
  moshi** (agents in moshi that are not on this Mac, each with "Running on <label>" where that is so) and
  **Machines** (below).
  Harnesses that `moshi-cockpit` does not find are greyed out with an Install button ("found by Herdr, not on
  moshi-cockpit's PATH" when only Herdr finds one); `grok` is not offered under New, it has no identity of its
  own.
- **Machines tab.** In the agents dialog's "+ Agent": this Mac and each Herdr machine with the six harnesses
  (`claude`, `codex`, `vibe`, `agy`, `grok`, `opencode`) as Ready, Installed, not signed in or Missing, with
  Install, Sign in and Refresh, and the hosts of `~/.ssh/config` with Add. "Where" in New starts an agent on
  another machine. A Linux machine's row also shows moshi-cockpit there: "moshi: Install", "moshi: Update" (only
  when its version is older than this Mac's), "moshi: Connect", "moshi ✓ <agents>" once connected, or "moshi: ?"
  with the reason in its tooltip. With a connected moshi-cockpit there, New and From moshi create or load the
  agent on that machine.
- **Spaces list.** A filter field, pinned spaces, groups and a right-click menu (Pin, Move to, New group…,
  Remove from, More…), in the cockpit layout only.
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
  nothing else. Opens the dialogs with `openOverlay`. A Linux machine's agent starts as `moshi-cockpit run
  <name>` in a tab (or a new space) on that machine.
- `mesh-agents`: the agents dialog, the start dialog and the connect dialog (`props.view`: `manage`, `new`,
  `start`, `connect`; the dialog also has the Machines tab). Runs `agent list|catalog|add|load|set|remove|where`,
  `sessions`, `machine list|inventory|hostkey|trust|remove`, `machine add --batch`, `machine hosts remove`,
  `machine cockpit` (moshi-cockpit's state on a Linux machine) and `machine agent add|load|catalog` (the same
  calls on that machine) through `exec`. It opens `run`, `connect`, `harness install`, `machine add` and
  `hc machine login` in a new tab on this Mac; on the chosen machine it opens a tab for a harness's install or
  sign-in command, the disconnect fix, and on a Linux machine moshi-cockpit's Install or Update (that release's
  `install.sh`), Connect (`moshi-cockpit connect`) and `moshi-cockpit run <name>`. It never touches a network
  or a token itself.
- `mesh-new-menu`: the "+" menu, derived from Herdr's own new-menu (Apache-2.0), plus the rows "Connect to
  moshi…" (only while not connected), "New agent…", "Manage agents…" and "Machine…". Opens `connect` in a new
  tab.
- `mesh-spaces`: the spaces list, derived from Herdr's own spaces mod (Apache-2.0), plus the filter, pins,
  groups and its right-click menu. Keeps pins and groups in `shared()`; runs `hc machine ls --json`; opens
  Herdr's own space menu for "More…".
- Herdr functions used: `exec`, `hc`, `home`, `props`, `agents`, `spaces`, `machines`, `panes`, `window`,
  `layout`, `theme`, `keys`, `knownAgents`, `shared`/`setShared`, `watchFolder`, `notify`, `openOverlay`,
  `closeOverlay`, and `run` with `tab.new` (for `run`, `connect`, `harness install`, `machine add`, installs,
  sign-ins, moshi-cockpit's Install, Update and Connect on a machine, and the standard agents), `agent.focus`,
  `space.new` (also on a machine), `space.select`, `space.move` and `app.settings`.
- Programs it runs: `~/.local/bin/moshi-cockpit`, which is the command line tool in this repository, and
  from mesh-spaces (inherited from Herdr's spaces list): `git rev-parse`, `git symbolic-ref` and `git rev-list`
  for each space's branch, here and on other machines; Herdr's `hc machine ls`; and, only when you reconnect a
  machine that was rebuilt, `hc machine rm` and `hc machine add … --name`. On a machine you pick, it opens the
  vendor's install or login command of a harness in a visible tab, after you confirm an install.
- Art: none. The package ships no images; the Vibe entry has no icon.
- Install: `curl -fsSL https://github.com/pcas-io/moshi-cockpit/releases/latest/download/install.sh | sh`
  copies the mods and the layout; then open Herdr and switch to the `cockpit` layout. On a Linux machine,
  Install in its Machines row runs the same installer headless (no Herdr there).

## Watch out

- Herdr must be installed first, from herdr.dev. The versions tested are listed in `cockpit/compat.py`; other
  versions work with a warning in `moshi-cockpit doctor`.
- Herdr on macOS on Apple silicon; Herdr's remote Linux machines (x86_64, aarch64) run moshi-cockpit headless.
  Herdr desktop on Linux comes later.
- Mods run with full rights and these run programs: read them before you add them. They never see a
  token; tokens live behind the CLI: in the Keychain on the Mac, and on a Linux machine in the Secret Service or
  a file with mode 0600, on that machine only. See `docs/security.md`.
- The cockpit layout is generated from Herdr's `main` layout at install time. If a later Herdr changes `main`,
  run `moshi-cockpit install` again. `mesh-spaces` and `mesh-new-menu` are copies of Herdr's own mods;
  `moshi-cockpit doctor` warns when Herdr changes the original.
- A harness started through "Where" on a machine without a connected moshi-cockpit runs without moshi.
- moshi and ocellio are optional. Without moshi there is no presence and no takeover; agents start locally.
