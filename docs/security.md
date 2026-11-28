# Security

What moshi-cockpit stores, where, and what it does and does not protect against. This describes the code in
this repository; if the two ever disagree, the code is right and this file is a bug.

## What is stored where

| Where | What |
|---|---|
| macOS Keychain, service `moshi-device`, account `device` | The device key: this installation's read-only identity in moshi. |
| Keychain, `moshi-register` / `register` | The registration token. Strict (see below). |
| Keychain, `moshi-<agent>` / `<agent>` | One moshi token per agent, for this installation. |
| Keychain, `ocellio-default` / `default` | Your ocellio token, if you use ocellio. |
| Keychain, `ocellio-<agent>` / `<agent>` | A copy of it for each agent. |
| `~/.config/moshi-cockpit/` | `config.json` (server addresses, which server each token is bound to, the registration token's expiry), `agents.json` (your agents), `rules.md` (your own rules), `hosts.json` (the targets you typed in the Machines tab). Files holding settings are written owner-only. |
| `~/.local/state/moshi-cockpit/` | `inventory.json`, the machine inventory cache (see Machines); `backups.json`, the list of every file the cockpit changed, so `uninstall` can restore them (the backup copies themselves sit next to each file as `<name>.bak-<date>-<time>`); `inventory.lock`, the lock that keeps two inventory runs from writing the cache at once; a marker that the earlier setup was imported; `locks/`, lock files that keep two rewrites of the same Keychain entry apart. |
| `~/.local/share/moshi-cockpit/` | The installed release, the pinned Python runtime, and per agent the files its harness needs (a Vibe profile, say). |
| `~/.gemini/config/mcp_config.json` | Only with an agy agent: its moshi and ocellio tokens, in plain text, mode 0600 (see below). |
| `~/.local/state/moshi-cockpit/remote-cockpit.json` (on the Mac) | Per Linux machine: moshi-cockpit's state there (OS, installed or connected, its version, which harnesses can take an agent, whether ocellio is set up) and each agent's name, harness and role; mode 0600, no token. `remote-cockpit.lock` next to it keeps two checks from writing it at once. |
| On a Linux machine: the Secret Service (libsecret), same service and account names as above | The same tokens as in the Keychain, for that machine's own installation, when the Secret Service works there (see [Tokens on Linux machines](#tokens-on-linux-machines)). |
| On a Linux machine: `~/.config/moshi-cockpit/secrets.json` | The same tokens in one file, mode 0600, when the Secret Service does not work there. |
| On a Linux machine: `~/.config/moshi-cockpit/secrets-store` | A marker (the words `secret-service`, no token) that keeps the Secret Service the store once it took a token. |

No token is written to an argument list, a log or an error message, and with one exception never to a file.
Keychain writes go through `security -i` on standard input; tokens reach an agent through its environment.
The exception is agy: it reads one global configuration file and sends header values as written, without
expanding variables, so its moshi and ocellio tokens sit in `~/.gemini/config/mcp_config.json`, written
owner-only (0600). `doctor` reports that file when its mode is wider.

Each token is bound to the server address it was issued for. A token is read from the Keychain only when the
configured server is that same origin; if the configuration points somewhere else, nothing is sent.

## Tokens on Linux machines

A Linux machine has no macOS Keychain. moshi-cockpit picks one of two stores there, with the same service and
account names as on the Mac. The first choice sticks, so tokens never end up split across two stores:

1. If `~/.config/moshi-cockpit/secrets.json` exists, that file, without trying anything else.
2. If `~/.config/moshi-cockpit/secrets-store` exists, the Secret Service (libsecret), again without a test.
   That marker holds only the words `secret-service` (no token) and is written, mode 0600, the first time the
   Secret Service takes a token. From then on the cockpit never switches to the file: if `secret-tool` later
   fails or does not answer within 10 seconds, the command ends with "The Secret Service did not answer.
   Unlock your keyring (or log in to a desktop session) and try again."
3. On a machine with neither, the Secret Service is tested once: `secret-tool` must be installed and a test
   entry must be stored, read back and cleared, each step within 5 seconds (the test entry is cleared even
   when reading it back fails). A keyring that is locked and would wait for an unlock prompt nobody sees on a
   headless machine fails that test, and the file is used instead, created on the first write.

Tokens reach `secret-tool` on standard input, never as an argument. Any call that hangs ends after 10 seconds
with an error that names the entry, never with a hang. An entry counts as missing only when `secret-tool`
ends with exit code 1 and prints nothing; any other failure (a locked keyring, no session bus) ends with the
"did not answer" sentence above, without `secret-tool`'s output.

Linux has no per-read approval: neither store asks before a read, so any process of that machine's user can
read the tokens there, including the registration token while it is valid (up to 24 hours). Strict storage
(see [Registration token](#registration-token)) exists only in the macOS Keychain.

The file holds `{"version": 1, "entries": {"<service>": {"<account>": "<token>"}}}` and is mode 0600; its
folder is created 0700 when new, and an existing folder keeps its mode. Every change rewrites the file whole:
under an exclusive lock (`.secrets.lock` next to it), into a temporary file in the same folder, synced, renamed
over the old one, then the folder is synced, so two commands writing at once both keep their entries. Before
reading, the cockpit opens it without following a symlink and refuses it, with one sentence that names the
path and the fix but never the content, when it is a symlink, not a regular file, owned by another user,
accessible to group or others, larger than 1 MiB, not valid JSON, or holds a value that is not a valid token.

To move from the file to libsecret by hand: delete `~/.config/moshi-cockpit/secrets.json` and connect again,
which stores new tokens in the Secret Service. To move the other way, delete `secrets-store` and the
moshi-cockpit entries in your keyring, then connect again. `doctor` names the store in use.

## Device key

The device key identifies this installation to moshi and is read-only: its only use is reading the
installation's status (who is active, who is online). It cannot create, load or change an agent and cannot
send a message. If it is removed in the moshi dashboard, the sidebar shows "Disconnected from moshi" and
connecting again creates a new installation.

## Registration token

Connecting gives the installation a registration token that creates and loads agents. It is stored strict:
the Keychain entry has an empty list of trusted applications, so macOS asks for your approval on every read. It
is valid for at most 24 hours (the expiry is kept in `config.json`). Once it has expired, creating or loading
an agent fails closed with exit code 3 and the mods reopen the connect dialog. Renewing it needs a new
approval in the browser, where you compare the code shown in the terminal tab with the one on the page.

## Agent tokens and installations

Every agent has its own token per installation, so one agent is one identity and an installation can be cut
off on its own. moshi accepts an agent's messages only from the installation that is active for that agent.
`moshi-cockpit agent remove` ends this installation's token for the agent first and then deletes the local
entries; if moshi cannot be told, the local removal still happens with a warning.

grok reads one global config, `~/.grok/config.toml`, where its login lives too. `install` adds one marked
block there (through the backup store, mode 0600) with the moshi server and the header
`Authorization: Bearer ${MESH_MOSHI_TOKEN}`: grok expands the variable when it loads the config, so no token is
written to the file. `run` sets the variable to the agent's own token, in the environment only; a plain `grok`
start has none and moshi refuses it. A `[mcp_servers.moshi]` you wrote yourself is left alone and reported by
`doctor`. `uninstall` restores the file as it was.

## Takeover

Starting an agent makes this installation its active one. If another installation holds it, the cockpit asks
(the panel and `run` in a terminal ask "Take over here?"; without a terminal `run` exits with code 4) and only an
explicit yes (or `--take-over`) goes on. Nothing is taken over before the start is prepared: an agent whose CLI
cannot be found stays where it is.
The limits:

- moshi names installations by label. Two installations with the same label look alike to the cockpit, so it
  cannot tell them apart.
- There is no compare-and-swap. The cockpit reads who holds the agent and then activates; if another
  installation takes over in between, the last activation wins.
- Takeover moves the right to receive messages. It does not stop the other installation's running process;
  moshi refuses that process's token from then on.

## Network behavior

Every request that carries a bearer token goes through one function that refuses redirects, so a token is
never forwarded to another origin, and caps a response body at 64 KiB. A page URL that moshi returns for the
browser step is opened only if it is `https` on the configured server's origin; otherwise the tab shows the
address and the code for you to open yourself.

## Installer integrity

The release installer carries the SHA-256 of the cockpit package and of the pinned Python build it
downloads. Downloads are HTTPS only (`--proto =https`, TLS 1.2 or newer). Both files are checked before
anything is unpacked or run; a mismatch stops the installer before it changes anything. The runtime is reused
only when the hash stored next to it matches; that hash is written only after the unpacked Python started, so
a runtime that cannot run here (a Linux host without glibc) is downloaded again next time. Herdr is never downloaded by the cockpit: you install it from
its vendor first. On a remote Linux machine, Install (and Update) in the Machines tab fetches the install.sh of
this Mac's version, from that release tag, over HTTPS only. That install.sh itself is not hash-checked: you
trust GitHub's HTTPS and the release for it. It then verifies Python and the payload by SHA-256 as above.

## Agent CLI installers

`moshi-cockpit harness install <name>` runs the vendor's own documented installer for that agent CLI. The
command is fixed in the code per harness, printed in full before anything runs, and runs only after you type
yes. The cockpit adds no installer a vendor does not publish.

## Machines and agent CLIs

`moshi-cockpit machine ...` and the Machines tab look at the machines Herdr reaches over ssh.

- **ssh config.** `~/.ssh/config` and the files it includes are read to list hosts Herdr does not know yet. The
  cockpit never writes them. To learn which host and port a target means, the add flow runs `ssh -G`, which
  evaluates your ssh config like ssh itself (a `Match exec` in it runs), so it is not a pure read; it dials
  nothing. A host that uses ProxyJump or ProxyCommand, a HostKeyAlias, known_hosts files other than the
  defaults, or `HashKnownHosts yes` gets no host key check and no trust in the dialog: it is offered "In a tab".
- **Own host list.** Targets typed in the Machines tab are kept in `~/.config/moshi-cockpit/hosts.json`
  (mode 0600, read only when it is owner-only). Each entry passes the name rule below before it is used.
- **Host keys.** Before adding, `ssh-keygen -F` asks whether the host's key is in `~/.ssh/known_hosts`. If not,
  `ssh-keyscan` fetches the keys the host offers, and the tab shows the type and SHA256 fingerprint of one.
  Nothing is written until you press Trust. Trust fetches the keys again and appends exactly the one line whose
  type and fingerprint are the ones shown, under a file lock; other lines are never rewritten. If the key changed
  in between, nothing is trusted; if the host no longer answers, nothing is trusted either. `~/.ssh` is created
  0700 and `known_hosts` 0600 when missing.
- **Adding.** The tab runs `hc machine add --install` without a terminal (standard input closed, a session of
  its own, a two-minute limit after which the whole process group is killed), so neither hc nor ssh can ask
  anything: herdr-core is installed on the machine without asking, and only key logins work. A machine that
  wants a password, accepts only keys none of yours match, or needs a login hc can't do alone gets the
  "In a tab" fallback: `machine add` attached to a terminal, where hc asks for the password or 2FA code.
  Passwords never pass through the cockpit. A `hosts.json` that is broken or not owner-only never blocks an
  add; the target is then just not saved.
- **Names.** A machine name or ssh alias reaches `hc` only if it starts with a letter or digit and holds
  letters, digits and `. _ @ : -`; anything else is refused before a process starts.
- **Inventory.** One fixed shell script is run on the machine (through `hc exec` for a remote one). Nothing
  is put into it. For each of the six agent CLIs it tests that the program exists and whether it is signed
  in, by an exit code (`claude auth status`, `codex login status`) or by whether a file exists (Vibe's
  `.env`, Grok's `auth.json`; for `claude` its `.credentials.json`, only when `claude auth status` cannot
  answer, that is exits with neither 0 nor 1). Where a vendor offers neither (`agy`, `opencode`) the state is unknown and
  shown as "Installed, not signed in". Output of the vendor commands is discarded; no credential file is
  read, and no token is in an argument list, the cache or a log.
- **Cache.** `~/.local/state/moshi-cockpit/inventory.json` (mode 0600) holds per machine the time, whether
  `curl` exists and the three states. It is used for at most a minute per machine; a failed check never
  replaces it. The inventory is asked only when the Machines tab opens, after `machine add` and on Refresh;
  nothing polls.
- **Installs on a remote machine.** The vendor's own one-liner (the same fixed commands as `harness install`)
  runs on the machine as the SSH login user, into that user's home directory, in a tab you can see, after you
  confirm the command in the dialog. Installs never use `sudo`. As with the local installers, the
  command is the vendor's pipe-to-shell command and you trust the vendor for it. Signing in runs the
  vendor's login command in a visible tab; you complete it there.
- **Disconnect fix.** The linger fix (`sudo loginctl enable-linger -- <user>`) runs with sudo only when you
  click Run in a tab, in a visible tab on that machine, and asks for your password there; the cockpit never
  sees, passes or stores it. The user comes from Herdr's machine list, must pass the name rule above, and is
  an argument of a fixed script, never shell text. Log in (`hc machine login`) likewise runs in a visible tab
  on this Mac, where hc asks.
- **Remote machines and moshi.** Agents on a Linux machine have their own moshi identity: the machine's
  moshi-cockpit is an installation of its own, connected with its own approval. Its device key, registration
  token and the agents' tokens stay on that machine, in the store `moshi-cockpit doctor` there names (see
  [Tokens on Linux machines](#tokens-on-linux-machines)); the Mac holds none of them. The Mac runs the
  machine's own moshi-cockpit through `hc exec`, with names, roles and skills as arguments of a fixed script,
  never as shell text, and reads back only exit codes, one-line notes, the version there, whether it is
  connected, and each agent's name, harness and role. Those it caches per machine in
  `~/.local/state/moshi-cockpit/remote-cockpit.json` (mode 0600, no token), which "Your agents" reads. A
  harness started through "Where" on a machine without a connected moshi-cockpit runs without moshi.
- **Connect on a Linux machine.** Connect runs `moshi-cockpit connect` in a tab on that machine: it prints the
  link and the code and opens no browser there; you approve on the Mac or your phone. If ocellio is set up,
  you type your ocellio token into that tab during Connect (hidden input): the keystrokes travel over Herdr's
  ssh connection to the machine, its moshi-cockpit stores the token there, and the cockpit on the Mac never
  reads or keeps it.

## Roster and ring

`roster` reads herdr-core's terminals on this machine and the label `agent=<name>` that `run` sets on its
terminal, and moshi's presence with the device key. `ring` is how one agent wakes another on this machine. It
never types free text, presses no keys and reads no screen:

- The message id must have moshi's shape (`msg_` and 26 characters), and moshi must confirm with the caller's
  own agent token (`mesh_get`) that the message exists and is addressed to the recipient. Without an own token
  (an agent started without `run`) ring refuses.
- The address comes from the roster only, never from a message: the one live terminal labelled with that name
  in which an agent reports its own status to herdr-core. herdr-core clears that report when the shell returns
  to the foreground, so a label left behind by an agent that has ended is ignored: a line pasted into a shell,
  a password prompt or a REPL would be submitted there. The terminal is checked once more right before the
  paste. Two terminals with the same name are refused.
- `run` looks for herdr-core only where it installs itself under this HOME (`~/.local/bin/hc`, the bundle),
  never on `PATH`.
- The line is `Moshi <id> — <type> from <sender>`: the type reduced to identifier characters, the sender
  without control characters, on one line.
- `--without-record` pastes the given text, marked `[no-record]` and reduced to one line without control
  characters, and only while moshi does not answer. The recipient cannot verify its sender.

The agent environment of codex, vibe and agy still drops the `HC_*` variables, and `run` passes the terminal
address as `COCKPIT_HC_ADDRESS` instead. This is hygiene, not a boundary: hc reaches the core over its default
socket without those variables, and any process of your user can call it, including `hc keys` into another
agent's terminal. The rules every agent gets tell it to ring through `moshi-cockpit ring` and nothing else.

## Pins and groups

The cockpit layout's spaces list keeps pins and groups in Herdr's shared store (space ids and your group
names, per machine), not in this repository, the Keychain or a cockpit file. Any Herdr mod can read that
store.

## Leak guard

Because the repository is public, `tools/leak_check.py` blocks internal identifiers, private email addresses,
issue references and a private list of blocked words. It scans tracked and new files and their paths, the
author, committer and message of every commit (GitHub's own merge commits keep their pull request number and
GitHub's address), text that becomes public outside git (pull request bodies,
release notes, comments) and branch and tag names. The word list is never part of the repository. The
pre-push hook and `tools/check.sh` run it, and without the list it fails closed.

## Residual risks

- A process of the same macOS user can read Keychain entries that are not strict (the device key, the agent
  tokens and the ocellio token) without a prompt. On a Linux machine any process of that user can read every
  token there, the registration token included (see [Tokens on Linux machines](#tokens-on-linux-machines)). With an agy agent, the same goes for its moshi and ocellio
  tokens in `~/.gemini/config/mcp_config.json`, which any process of your user can read. Only the registration token asks every time. The cockpit
  does not isolate agents from you: an agent runs as your user.
- Herdr mods run with full rights in your Herdr window. The cockpit ships its own and `doctor` warns about any
  mod it does not know, but it cannot vouch for mods you add.
- Herdr updates itself. A new version it was not tested with only produces a `doctor` warning.
- The vendor installers behind `harness install` and the Install cells of the Machines tab are the vendors' own pipe-to-shell commands; you trust them
  the way you would if you ran them by hand.
- The leak guard only knows the words on your private list.
- Any process of your user can call herdr-core (`hc`) directly, also from an agent whose environment dropped
  the `HC_*` variables, and press keys in another terminal. `ring` narrows what the cockpit's own rules ask
  agents to do; it does not take hc away from them. Labels can be set by the same processes, so a label says
  where an agent says it runs, not who it is.

## Reporting a problem

Report security issues privately through GitHub's private vulnerability reporting (Security tab → Report a
vulnerability) — please don't open a public issue.
