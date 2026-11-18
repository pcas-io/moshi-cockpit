# moshi-cockpit

Herdr mods and a command line tool for running coding agents on moshi, an async message mesh for AI
agents. It is built for people who run several agents and want each one to have
its own identity, a safe default setup and one place to start, move and watch them. moshi is optional; so is
ocellio, a knowledge graph the agents can read. Without either, the cockpit still manages and starts your
agents locally. Agents get their ocellio token through `connect`, which needs moshi for now: ocellio without
moshi is not supported yet.

What you get inside [Herdr](https://herdr.dev):

- a "cockpit" layout with a sidebar list of your agents and their presence,
- a "+ Agent" dialog to create an agent, load one that exists in moshi, or install a missing agent CLI,
- takeover between installations: an agent is active on one installation at a time,
- a "Machines" tab that shows which agent CLIs are installed and signed in on this Mac and on the machines
  Herdr reaches over ssh, and installs or signs in from there,
- a spaces list for the cockpit layout with a filter, pinned spaces and named groups,
- the `moshi-cockpit` command line tool behind all of it, usable on its own.

See [docs/security.md](docs/security.md) for what is stored where and what the cockpit does not protect
against, and [RECIPE.md](RECIPE.md) for the Herdr mod gallery entry.

## Requirements

- macOS on Apple silicon; remote Linux machines (x86_64, aarch64) headless. Windows follows.
- Herdr, installed first from [herdr.dev](https://herdr.dev). The cockpit never downloads Herdr. The Herdr
  versions this release was tested with are listed in `cockpit/compat.py`; any other version only produces a
  warning in `moshi-cockpit doctor`.
- `curl`, `shasum`, `tar` (all part of macOS); on a Linux machine `curl`, `tar` and `sha256sum` (or `shasum`).
- Optional: an address for moshi and one for ocellio. The installer asks for them; leave them empty to use
  Herdr without them. On an update it shows the address set now, and an empty answer keeps it.

## Install

```sh
curl -fsSL https://github.com/pcas-io/moshi-cockpit/releases/latest/download/install.sh | sh
```

The `install.sh` of a release installs exactly that release: it carries the version and the SHA-256 of
everything it downloads, and checks both downloads before unpacking or running anything. It puts a pinned
Python and the cockpit under `~/.local/share/moshi-cockpit/`, links `~/.local/bin/moshi-cockpit` and runs
`moshi-cockpit install`. If `~/.local/bin` is not on your `PATH`, the installer says so.

Flags go after `sh -s --`, for example to set the servers without being asked:

```sh
curl -fsSL https://github.com/pcas-io/moshi-cockpit/releases/latest/download/install.sh \
  | sh -s -- --moshi https://moshi.example.com/mcp --ocellio https://ocellio.example.com/mcp/
```

`install` is idempotent and every file change goes through a backup, so `moshi-cockpit uninstall` can restore
it. It switches Herdr's telemetry off, installs the mods, the cockpit layout and the launchers, and sets up
the agents you already have. If Herdr has never run on the Mac, it starts Herdr once and waits until it is
ready, then switches to the cockpit layout (or tells you to press the layout key).

## First start

1. Open Herdr. If moshi is set up and this Mac is not connected yet, the connect dialog opens once per Herdr
   start; after that the "+" menu shows "Connect to moshi…" and the sidebar shows one line to the same effect.
2. Choose Connect. A tab opens and the cockpit starts moshi's device flow: your browser opens the approval page on
   your moshi, and the tab shows the same code. Compare the two codes. If the browser opened on another
   device, the tab names the page and the code to enter there. Approve in the browser.
3. The tab says `Connected as "<label>"`. The cockpit now holds a device key (read-only) and a
   registration token (valid for at most 24 hours) in the macOS Keychain.
4. If ocellio is set up and its token is missing, the tab asks for it once, with hidden input.
5. The tab ends with `Done. Back in Herdr, add your first agent with "+ Agent".` and closes when you press
   Enter.

When the registration token has expired, or the device key is missing or was revoked in the moshi dashboard,
creating or loading an agent reopens the same dialog; the installation and the agents' own tokens stay.

## Add an agent

"+ Agent" (in the "+" menu as "New agent…", or in the agents dialog) has three tabs, **New**, **From moshi**
and **Machines** (see [Machines](#machines)):

- **New:** a name (letters, digits, `_` and `-`), a harness, and optionally a role and an ocellio skill the
  agent loads at its start. Harnesses are `claude`, `codex`, `vibe` and `agy`; `grok` connects through your
  account and has no identity of its own, so it is not offered here (start it from "+" → Grok). A harness
  that `moshi-cockpit` does not find is greyed out with an **Install** button; if Herdr finds it but
  `moshi-cockpit` does not, the note says "found by Herdr, not on moshi-cockpit's PATH". Install opens a tab
  that shows the vendor's own installer command, runs it only after you type yes, and waits for Enter at the
  end. If moshi is set up, the agent is created there too and this installation
  gets a token of its own for it.
- **From moshi:** agents that exist in moshi and are not on this Mac yet. Pick one to load it. Agents that run
  elsewhere are marked "Running on <installation>".

Start an agent from the sidebar or the menu; the session picker lets you resume an earlier session.

**Where** (top of New) chooses this Mac or one of Herdr's machines whose layout Herdr has read. On another
machine the dialog lists the six harnesses from that machine's inventory. Start opens a new space on that
machine running the harness; a harness that is installed but not signed in can be started, its first start
signs in. Missing ones point to the Machines tab. Nothing is created in moshi or in `agents.json` for it: no
moshi runs on those machines in this release, and "Your agents" stays the moshi agents on this Mac.

Each agent's Vibe profile (`config.toml`) is written by the cockpit: hand edits to it are replaced on the next
`install` and whenever the agent's setup is written again.

## Machines

The "+" menu has a row **Machine…** (below "Manage agents…"); it opens "+ Agent" on the **Machines** tab. In the
cockpit layout, **Add machine** in the sidebar footer opens the Machines tab too (instead of Herdr's own form). The
tab is a compact grid: one row per machine (this Mac first, then each machine of Herdr's machine list), one
column per harness (`claude`, `codex`, `vibe`, `agy`, `grok` and `opencode`). Each cell shows one of three states,
as the legend next to the tab buttons says:

- **● Ready:** found, and signed in.
- **◐ Sign in** (installed, not signed in): found, and not signed in or not known to be (not every vendor offers
  a way to tell, so unknown counts here).
- **○ Install** (missing): not found.

Clicking a cell does what its symbol says (its tooltip names the state):

- **Install.** On this Mac it runs `moshi-cockpit harness install`, which asks y/N in its tab. On another
  machine the dialog shows the vendor's command under the grid first and runs it only after you confirm, in a
  new tab on that machine, as your SSH login user, into your home directory, never with `sudo`. On a machine
  without `curl` the cell stays empty and the row says "needs curl".
- **Sign in.** Opens a tab on that machine with the vendor's login command: `claude auth login`,
  `codex login --device-auth`, `vibe --setup`, `agy` (its first start signs in), `grok login --device-auth` or
  `opencode auth login`.

A machine that is not connected, or that Herdr is still connecting to, shows a short note in its row instead
of the cells. The **×** at a machine's row end removes it from Herdr (`hc machine rm`) after you confirm; its
terminals keep running there, and a saved target stays as a chip to add it again.

- **Warnings.** A machine warning in the spaces list (⚠, or a login it needs) opens this tab with that machine's row highlighted.
- **Log in.** A machine that needs a login says so in its row; **Log in** opens a tab on this Mac with `hc machine login`, where hc asks for the password or code; the row's **Refresh** checks it again afterwards.
- **Fix.** A machine where agents stop when you disconnect says so in its row; **Fix** shows `sudo loginctl enable-linger -- <user>` under the grid and runs it only on **Run in a tab**, in a tab on that machine where sudo asks for your password (**Copy** copies it instead).

Under the grid you add machines without a terminal. Herdr's own "Add a machine" can't answer ssh's
questions (host key, password, installing herdr-core), so the tab does it:

- **Chips.** "From ~/.ssh/config" lists the hosts Herdr does not know yet, "Saved" the targets you typed earlier
  (kept in `~/.config/moshi-cockpit/hosts.json`; the small × forgets one). A chip starts the add.
- **Add a machine** takes a target you type (`user@host`, `user@host:port` or an ssh alias), so a host needs no
  entry in `~/.ssh/config`. Enter or **Add** starts the add; the target is saved once hc is asked to add it
  (an ssh alias is not saved).
- **Host key.** When the machine's key is not in `~/.ssh/known_hosts`, the tab shows its type and SHA256
  fingerprint and asks **Trust** or **Cancel**. Trust checks the key again and adds only that key's line to
  `~/.ssh/known_hosts`; if it changed in between, nothing is trusted.
- **Adding.** `hc machine add --install` then runs without a terminal: it installs herdr-core there without
  asking. The new machine's six states appear in the grid.
- **Passwords.** The add logs in with ssh keys only. A machine that asks for a password shows "asks for a
  password" and **In a tab**, which runs `moshi-cockpit machine add` in a tab where hc asks for it. Passwords
  never pass through the cockpit. **In a tab** is also offered when none of your keys work, and for a host
  whose ssh settings the dialog doesn't follow exactly (ProxyJump or ProxyCommand, HostKeyAlias, other
  known_hosts files, `HashKnownHosts yes`, as `ssh -G` reports them).

The inventory is asked when the tab opens (a machine checked less than a minute ago is shown from the cache),
after `machine add`, and when you press **Refresh** next to the time a machine was last checked. Nothing polls. A
machine that does not answer shows one error line and keeps its last good result in the cache. Machines that are
not connected are listed and checked once Herdr reports them connected while the tab is open, or the next time it
opens. A machine added a moment ago says that Herdr is still starting herdr-core there; if the dialog closed
during an add, the next Machines tab shows how it ended.

## Linux machines

A Linux machine that Herdr on your Mac reaches runs moshi-cockpit headless: no Herdr app there, its agents get
their own moshi identity and you start them from Herdr on the Mac.

1. Add the machine under **Machines** (see above).
2. **Install moshi-cockpit** in its row. A tab on that machine runs the same install line as on the Mac; it picks
   the pinned Python for x86_64 or aarch64, checks every download's SHA-256 and puts everything under `~/.local`,
   without `sudo`.
3. **Connect to moshi** in its row. The tab there prints a link and a code; approve it in the browser on your Mac
   or phone. The installation is named after the machine.
4. **+ Agent → New** with **Where** set to that machine creates an agent there.

Tokens on a Linux machine go to the Secret Service (libsecret) when it works, otherwise to
`~/.config/moshi-cockpit/secrets.json` with mode 0600; `moshi-cockpit doctor` there names the store. See
[docs/security.md](docs/security.md#tokens-on-linux-machines).

## Spaces

In the cockpit layout, Herdr's spaces list is replaced by `mesh-spaces`, a copy of it with these additions:

- a **Filter spaces** field above the list: case-insensitive match on a space's name, folder, branch or
  machine;
- **pinned** spaces first, then your **groups**, then the rest; a group can be folded;
- a right-click menu on a space: **Pin** or **Unpin**, **Move to** a group of the same machine, **New group…**
  (asks for a name), **Remove from** its group, and **More…** for Herdr's own space menu;
- drag a space onto a group (or into the pinned spaces, or back to the rest).

Pins and groups are kept per machine in Herdr's shared store. A pinned space is in no group; a worktree goes
where its checkout goes. When a space is gone its entry is dropped, but not while its machine is offline.
The `main` layout and Herdr's own spaces mod stay as they are; switch to `main` for Herdr's plain list.
`moshi-cockpit doctor` warns when Herdr has changed the spaces mod this copy was made from.

## Takeover between installations

An agent token is bound to one installation, and moshi accepts messages for an agent only from the
installation that is active for it. Starting an agent here makes this installation the active one. If another
installation holds it, the cockpit asks first: "Running on <label>. Take over here?" **Take over** starts it
here and, from then on, the messages arrive here only. **Cancel** starts nothing. `run` in a terminal asks the
same question; without a terminal it exits with `4`, and `run --take-over` starts it here. Nothing is taken over
before the start is ready: an agent whose CLI cannot be found stays where it is. In a Herdr tab, a failed start
waits for Enter so you can read why. See [docs/security.md](docs/security.md) for what takeover does and does not guarantee.

If moshi cannot be reached when you start an agent, the agent still starts locally with one warning line,
and activation is skipped.

## Command line

`moshi-cockpit [--version] <command>`. Output never contains a token.

| Command | What it does |
|---|---|
| `install [--moshi URL] [--ocellio URL]` | Set up the cockpit in the Herdr you installed. A URL is the server's address (`https://…/mcp`; an address without a path gets `/mcp`, or `/mcp/` for ocellio); an empty string (`""`) removes it. Without a flag a setting stays as it is. |
| `uninstall` | Restore every change from the backups. Your settings in `~/.config/moshi-cockpit/`, the Keychain entries and Herdr itself stay. |
| `connect` | Connect this Herdr to moshi with one approval in the browser. |
| `migrate` | Move the agents of an earlier setup to this installation (after `connect`). |
| `run [--take-over] AGENT [ARGS…]` | Start an agent through its harness. `--take-over` starts it here even when another installation holds it. Arguments after the name go to the harness. |
| `doctor [--json] [--quiet]` | Check the setup. Exit code is not 0 on errors. `--quiet` prints nothing unless there is an error, then one sentence with one action. |
| `status [--json]` | moshi mesh overview, read with this Herdr's device key. `--json` is compact output for the sidebar. |
| `agent add [NAME] [--harness H] [--role TEXT] [--skill NAME]` | Create an agent (in moshi too, when set up). With a name and `--harness` it runs without a terminal; otherwise it asks. |
| `agent load NAME` | Add an agent that exists in moshi to this Mac. |
| `agent catalog [--json]` | moshi's agents that are not on this Mac yet. |
| `agent where NAME [--json]` | The installation an agent is active on. |
| `agent set NAME [--role TEXT] [--skill NAME]` | Change an agent's role or skill, here and in moshi. An empty value deletes it. |
| `agent list [--json]` | Show the agents. |
| `agent remove NAME` | Remove an agent from this Mac. It stays in moshi, and this installation's token for it ends. |
| `rules show` | Show your own rules for every agent. |
| `rules set [--text TEXT]` | Set your own rules (from stdin without `--text`; empty removes them) and set the agents up again. `install` never overwrites them. |
| `harness install NAME` | Install a missing agent CLI (`claude`, `codex`, `vibe`, `agy`, `grok`, `opencode`) on this Mac with the vendor's official installer, after showing the command and asking. |
| `machine list [--json]` | Herdr's machines (name, state, target), the hosts in `~/.ssh/config` and the saved hosts that Herdr does not know yet. |
| `machine inventory [--on NAME] [--refresh] [--json]` | The six agent CLIs on this Mac, or on the machine `NAME` from `machine list`, each Ready, Installed, not signed in, or Missing. A result younger than a minute comes from the cache; `--refresh` asks again. |
| `machine add ALIAS` | Add an ssh host (a `Host` from `~/.ssh/config`, `user@host` or `user@host:port`) to Herdr with `hc machine add`, attached to your terminal, then show its inventory. |
| `machine add TARGET --batch [--json]` | The same without a terminal (the Machines tab): saves the target, installs herdr-core there without asking and answers with the new machine's inventory, or that it needs a password or a host key. |
| `machine remove NAME [--json]` | Remove a machine from Herdr with `hc machine rm`; its terminals keep running there. Your saved hosts stay. |
| `machine hosts add\|remove TARGET` | Add a target to your saved hosts, or forget it. |
| `machine hostkey TARGET [--json]` | Whether the host's key is in `~/.ssh/known_hosts`, else the keys it offers with their SHA256 fingerprints; a host with ssh settings the dialog doesn't handle gets `needs: "tab"` instead. |
| `machine trust TARGET --type TYPE --fingerprint SHA256:… [--json]` | Check the host's key again and add it to `~/.ssh/known_hosts` only if type and fingerprint still match. |
| `sessions [AGENT] [--harness H] [--folder DIR] [--limit N] [--json]` | An agent's sessions that can be resumed. Give either an agent or `--harness`. `--limit` defaults to 8. |

### Exit codes

| Code | Meaning |
|---|---|
| `0` | Success. |
| `1` | Error; the message says what to do. |
| `3` | Approval required: the registration token is missing, expired or refused, or the device key is missing or revoked. Run `moshi-cockpit connect`. The mods reopen the connect dialog on this code. |
| `4` | The agent is active on another installation. Without a terminal, standard output is JSON, `{"active": "<label>"}`; use `--take-over` to start it here. In a terminal `run` asks instead and exits with `4` on a no. |

An interrupted command exits with `130`.

## Uninstall

```sh
moshi-cockpit uninstall
```

restores every file the cockpit changed from its backups, including the link in `~/.local/bin`. A file whose
backup is missing is left as it is and named in the output. What stays, so that a later install picks up where
you left off:

- your settings in `~/.config/moshi-cockpit/` (`config.json`, `agents.json`, `rules.md`),
- the Keychain entries (`moshi-…` and `ocellio-…`, see [docs/security.md](docs/security.md)); on a Linux machine
  the tokens in its store (`secrets.json` or your keyring),
- the downloaded app in `~/.local/share/moshi-cockpit/` and the state folder `~/.local/state/moshi-cockpit/`,
- Herdr itself.

To remove them by hand: `rm -rf ~/.config/moshi-cockpit ~/.local/share/moshi-cockpit ~/.local/state/moshi-cockpit`,
delete the `moshi-…` and `ocellio-…` entries in Keychain Access, and revoke the installation in the moshi
dashboard.

## Moving an earlier setup

If you ran the cockpit under its earlier name, `install` finds that setup and imports it once: it undoes the
earlier changes to Herdr (its backup files are moved back into place and its list of changes is cleared),
then carries over servers, agents, your own rules and the ocellio token. Its config folder is only read.
Then:

1. `moshi-cockpit connect` (or "Connect to moshi…" in Herdr), approve in the browser.
2. `moshi-cockpit migrate` adopts each agent's earlier token for this installation. The tokens themselves
   stay; the old helper entries go. Run it again at any time: agents that are already on this installation are reported as such.
3. In the moshi dashboard, revoke the old helper identities.

Until you have connected, agents do not start and the command exits with `3`. Agents whose harness runs
without moshi are not affected.

## Development

Python 3.9 or newer, standard library only. Before every push:

```sh
sh tools/check.sh
```

It runs the leak guard (`tools/leak_check.py`, on files and commit metadata), the Python tests on
`/usr/bin/python3` and `python3.12`, the mods' JS tests (`node --test`) and `shellcheck`.

To run it on every push, enable the repo's hooks once per clone:

```sh
git config core.hooksPath tools/hooks
```

The leak guard needs a private word list that is never part of the repo: one word per line in
`~/.config/moshi-cockpit-dev/blocked-words.txt`, or in the file named by `MOSHI_COCKPIT_BLOCKED_WORDS`.
Without it the guard stops with exit code 2.

## Publishing

The repo is public, so text that becomes public outside git is checked too. Every PR body, release note,
comment or repo description is written to a file and passed through the leak guard before `gh` sends it:

```sh
python3 tools/leak_check.py --text body.md   # `-` reads stdin
gh pr create --body-file body.md
```

Any finding blocks publishing; fix the text and run it again. Branch and tag names are checked the same way
(`--refs NAME...`), and the pre-push hook does it for every ref being pushed.

## License

Apache-2.0, see [LICENSE](LICENSE).
