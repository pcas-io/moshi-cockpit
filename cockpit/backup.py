"""Every change to the system goes through BackupStore so `uninstall` can undo it."""

from __future__ import annotations

import json
import os
import shutil
import time
from pathlib import Path
from typing import Callable, Dict, List, Optional


class BackupStore:
    def __init__(self, state_dir: Path, clock: Callable[[], float] = time.time) -> None:
        self._state_dir = state_dir
        self._manifest = state_dir / "backups.json"
        self._clock = clock

    def write_text(self, path: Path, text: str, mode: Optional[int] = None) -> bool:
        """Writes `text` to `path` if it differs. Returns True when the file changed."""
        if path.is_file() and not path.is_symlink() and path.read_text() == text:
            if mode is not None:
                os.chmod(path, mode)
            return False
        self._remember(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(path.name + ".tmp-moshi-cockpit")
        fd = os.open(str(tmp), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, mode if mode is not None else 0o644)
        with os.fdopen(fd, "w") as handle:
            handle.write(text)
        if mode is not None:
            os.chmod(tmp, mode)
        os.replace(tmp, path)
        return True

    def symlink(self, target: Path, link: Path) -> bool:
        """Points `link` at `target`. Returns True when the link changed."""
        if link.is_symlink() and Path(os.readlink(link)) == target:
            return False
        self._remember(link)
        link.parent.mkdir(parents=True, exist_ok=True)
        if link.is_symlink() or link.exists():
            link.unlink()
        link.symlink_to(target)
        return True

    def restore_all(self, keep: Optional[Path] = None) -> List[str]:
        """Reverts every recorded change, newest first, except those inside `keep` (they stay as they are and
        leave the list). Returns human-readable notes."""
        notes: List[str] = []
        for entry in reversed(self._load()):
            path = Path(str(entry["path"]))
            if keep is not None and (path == keep or keep in path.parents):
                continue
            notes.append(self._restore(entry))
        self._save([])
        return notes

    def entries(self) -> List[Dict[str, Optional[str]]]:
        return self._load()

    def _remember(self, path: Path) -> None:
        entries = self._load()
        if any(e["path"] == str(path) for e in entries):
            return  # the first backup is the original state; later ones would overwrite it
        backup: Optional[str] = None
        if path.is_symlink():
            backup = "symlink:" + os.readlink(path)
        elif path.exists():
            stamp = time.strftime("%Y%m%d-%H%M%S", time.localtime(self._clock()))
            target = path.with_name(f"{path.name}.bak-{stamp}")
            shutil.copy2(path, target)  # keeps mode, so a 0600 secret file stays 0600
            backup = str(target)
        entries.append({"path": str(path), "backup": backup})
        self._save(entries)

    def _restore(self, entry: Dict[str, Optional[str]]) -> str:
        path = Path(str(entry["path"]))
        backup = entry["backup"]
        if backup is not None and not backup.startswith("symlink:") and not Path(backup).is_file():
            return f"skipped, its backup is missing: {path}"  # never delete a file that cannot be put back
        if path.is_symlink() or path.exists():
            path.unlink()
        if backup is None:
            return f"removed: {path}"
        if backup.startswith("symlink:"):
            path.symlink_to(backup[len("symlink:"):])
            return f"symlink restored: {path}"
        shutil.move(backup, path)
        return f"restored: {path}"

    def _load(self) -> List[Dict[str, Optional[str]]]:
        if not self._manifest.is_file():
            return []
        return json.loads(self._manifest.read_text())

    def _save(self, entries: List[Dict[str, Optional[str]]]) -> None:
        self._state_dir.mkdir(parents=True, exist_ok=True)
        os.chmod(self._state_dir, 0o700)
        self._manifest.write_text(json.dumps(entries, indent=2))
