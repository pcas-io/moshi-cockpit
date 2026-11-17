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
| `~/.config/moshi-cockpit/` | `config.json` (server addresses, which server each token is bound to, the registration token's expiry), `agents.json` (your agents), `rules.md` (your own rules). Files holding settings are written owner-only. |
| `~/.local/state/moshi-cockpit/` | `backups.json`, the list of every file the cockpit changed, so `uninstall` can restore them (the backup copies themselves sit next to each file as `<name>.bak-<date>-<time>`); a marker that the earlier setup was imported; `locks/`, lock files that keep two rewrites of the same Keychain entry apart. |
| `~/.local/share/moshi-cockpit/` | The installed release, the pinned Python runtime, and per agent the files its harness needs (a Vibe profile, say). |
| `~/.gemini/config/mcp_config.json` | Only with an agy agent: its moshi and ocellio tokens, in plain text, mode 0600 (see below). |

No token is written to an argument list, a log or an error message, and with one exception never to a file.
Keychain writes go through `security -i` on standard input; tokens reach an agent through its environment.
The exception is agy: it reads one global configuration file and sends header values as written, without
expanding variables, so its moshi and ocellio tokens sit in `~/.gemini/config/mcp_config.json`, written
owner-only (0600). `doctor` reports that file when its mode is wider.

Each token is bound to the server address it was issued for. A token is read from the Keychain only when the
configured server is that same origin; if the configuration points somewhere else, nothing is sent.

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
only when the hash stored next to it matches. Herdr is never downloaded by the cockpit: you install it from
its vendor first.

## Agent CLI installers

`moshi-cockpit harness install <name>` runs the vendor's own documented installer for that agent CLI. The
command is fixed in the code per harness, printed in full before anything runs, and runs only after you type
yes. The cockpit adds no installer a vendor does not publish.

## Leak guard

Because the repository is public, `tools/leak_check.py` blocks internal identifiers, private email addresses,
issue references and a private list of blocked words. It scans tracked and new files and their paths, the
author, committer and message of every commit (GitHub's own merge commits keep their pull request number and
GitHub's address), text that becomes public outside git (pull request bodies,
release notes, comments) and branch and tag names. The word list is never part of the repository. The
pre-push hook and `tools/check.sh` run it, and without the list it fails closed.

## Residual risks

- A process of the same macOS user can read Keychain entries that are not strict (the device key, the agent
  tokens and the ocellio token) without a prompt. With an agy agent, the same goes for its moshi and ocellio
  tokens in `~/.gemini/config/mcp_config.json`, which any process of your user can read. Only the registration token asks every time. The cockpit
  does not isolate agents from you: an agent runs as your user.
- Herdr mods run with full rights in your Herdr window. The cockpit ships its own and `doctor` warns about any
  mod it does not know, but it cannot vouch for mods you add.
- Herdr updates itself. A new version it was not tested with only produces a `doctor` warning.
- The vendor installers behind `harness install` are the vendors' own pipe-to-shell commands; you trust them
  the way you would if you ran them by hand.
- The leak guard only knows the words on your private list.

## Reporting a problem

Report security issues privately through GitHub's private vulnerability reporting (Security tab → Report a
vulnerability) — please don't open a public issue.
