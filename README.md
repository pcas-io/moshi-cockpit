# moshi-cockpit

Herdr mods and a CLI for running coding agents on moshi.

Work in progress: the installer, docs and the first release follow.

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
