"""The own host list: targets typed in the Machines tab (`user@host`, `user@host:port`, …) that are not in
~/.ssh/config, so they can be added again with one click.

`~/.config/moshi-cockpit/hosts.json`: {"hosts": ["deploy@build-1", ...]}, owner-only like config.json and
written through BackupStore (uninstall keeps the settings folder). Every entry passes `check_target`; an entry
that does not is skipped when read.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import List

from cockpit.backup import BackupStore
from cockpit.config import read_private
from cockpit.paths import Paths
from cockpit.targets import check_target


def hosts_file(paths: Paths) -> Path:
    return paths.mesh_config.parent / "hosts.json"


def load_hosts(paths: Paths) -> List[str]:
    """The saved targets in order; [] without a file, ValueError for a file that is broken or not owner-only."""
    path = hosts_file(paths)
    if not path.exists() and not path.is_symlink():
        return []
    try:
        found = json.loads(read_private(path, "saved hosts")).get("hosts")
    except (ValueError, AttributeError):
        found = None
    if not isinstance(found, list):
        raise ValueError(f"{path} is not valid; remove it and add the hosts again")
    result: List[str] = []
    for entry in found:
        try:
            target = check_target(entry)
        except ValueError:
            continue
        if target not in result:
            result.append(target)
    return result


def _save(paths: Paths, store: BackupStore, targets: List[str]) -> None:
    store.write_text(hosts_file(paths), json.dumps({"hosts": targets}, indent=2) + "\n", mode=0o600)


def add_host(paths: Paths, store: BackupStore, target: str) -> bool:
    """Appends `target`; False when it is saved already."""
    check_target(target)
    targets = load_hosts(paths)
    if target in targets:
        return False
    _save(paths, store, targets + [target])
    return True


def remove_host(paths: Paths, store: BackupStore, target: str) -> bool:
    """Drops `target`; False when it was not saved."""
    check_target(target)
    targets = load_hosts(paths)
    if target not in targets:
        return False
    _save(paths, store, [t for t in targets if t != target])
    return True
