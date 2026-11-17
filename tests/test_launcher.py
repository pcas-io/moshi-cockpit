"""bin/moshi-cockpit picks its Python: the bundled runtime for a release under ~/.local/share/moshi-cockpit/app,
python3 from PATH anywhere else, and MOSHI_COCKPIT_PYTHON over both. Each Python here is a stub that only says
which one it is."""

from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SYSTEM_PATH = "/usr/bin:/bin:/usr/sbin:/sbin"


def stub(path: Path, name: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f"#!/bin/sh\necho {name}\n")
    path.chmod(0o755)


class LauncherTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tmp = Path(os.path.realpath(tmp.name))
        self.home = self.tmp / "home"
        self.data = self.home / ".local" / "share" / "moshi-cockpit"
        self.path_dir = self.tmp / "path"
        stub(self.path_dir / "python3", "path-python")

    def install_launcher(self, root: Path) -> Path:
        launcher = root / "bin" / "moshi-cockpit"
        launcher.parent.mkdir(parents=True)
        shutil.copy2(ROOT / "bin" / "moshi-cockpit", launcher)
        return launcher

    def run_launcher(self, launcher: Path, **extra: str) -> str:
        env = {"PATH": f"{self.path_dir}:{SYSTEM_PATH}", "HOME": str(self.home), **extra}
        done = subprocess.run([str(launcher), "--version"], capture_output=True, text=True, env=env, timeout=30)
        self.assertEqual(done.returncode, 0, done.stderr)
        return done.stdout.strip()

    def test_a_release_uses_the_bundled_runtime(self):
        stub(self.data / "runtime" / "python" / "bin" / "python3", "bundled-python")
        launcher = self.install_launcher(self.data / "app" / "1.2.3")
        self.assertEqual(self.run_launcher(launcher), "bundled-python")

    def test_a_release_reached_through_the_link_in_local_bin_uses_the_bundled_runtime(self):
        stub(self.data / "runtime" / "python" / "bin" / "python3", "bundled-python")
        launcher = self.install_launcher(self.data / "app" / "1.2.3")
        link = self.home / ".local" / "bin" / "moshi-cockpit"
        link.parent.mkdir(parents=True)
        link.symlink_to(launcher)
        self.assertEqual(self.run_launcher(link), "bundled-python")

    def test_a_release_without_a_runtime_falls_back_to_python3(self):
        launcher = self.install_launcher(self.data / "app" / "1.2.3")
        self.assertEqual(self.run_launcher(launcher), "path-python")

    def test_a_checkout_never_uses_the_bundled_runtime(self):
        stub(self.data / "runtime" / "python" / "bin" / "python3", "bundled-python")
        launcher = self.install_launcher(self.tmp / "checkout")
        self.assertEqual(self.run_launcher(launcher), "path-python")

    def test_moshi_cockpit_python_wins(self):
        stub(self.data / "runtime" / "python" / "bin" / "python3", "bundled-python")
        stub(self.tmp / "chosen" / "python3", "chosen-python")
        launcher = self.install_launcher(self.data / "app" / "1.2.3")
        self.assertEqual(self.run_launcher(launcher, MOSHI_COCKPIT_PYTHON=str(self.tmp / "chosen" / "python3")),
                         "chosen-python")


if __name__ == "__main__":
    unittest.main()
