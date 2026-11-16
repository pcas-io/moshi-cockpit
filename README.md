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

## License

Apache-2.0, see [LICENSE](LICENSE).
