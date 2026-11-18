"""tools/release.sh: refuses unless the tree is clean, HEAD is the tag, the tag is on origin/main, the version
matches and the checks pass; builds the payload, the rendered installer, SHA256SUMS and the notes, runs the
leak guard over the notes and creates the release only after a yes on a terminal, and only when origin holds the
same tag.

`git`, `gh` and tools/check.sh are fakes in a copy of the tools: nothing real is called or published."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import tarfile
import tempfile
import threading
import unittest
from pathlib import Path
from typing import List

ROOT = Path(__file__).resolve().parent.parent
SYSTEM_PATH = "/usr/bin:/bin:/usr/sbin:/sbin"
VERSION = "1.2.3"
TAG = "v" + VERSION
COMMIT = "1" * 40
OTHER = "2" * 40
WORD = "zebracornia"  # stands in for an entry of the private word list

# Answers the git questions release.sh asks, from a JSON state file; logs every call.
FAKE_GIT = r'''#!/usr/bin/python3
import io, json, os, sys, tarfile
state = json.load(open(os.environ["FAKE_STATE"]))
args = sys.argv[1:]
with open(os.environ["FAKE_LOG"], "a") as log:
    log.write(json.dumps(["git"] + args) + "\n")
if args[:2] == ["status", "--porcelain"]:
    sys.stdout.write(state["status"])
elif args[:1] == ["rev-parse"] and args[-1] == "HEAD":
    print(state["head"])
elif args[:1] == ["rev-parse"]:
    if state["tag"] is None or not args[-1].startswith("refs/tags/" + state["tag"]):
        sys.exit(1)
    print(state["tag_commit"])
elif args[:1] == ["fetch"]:
    pass
elif args[:1] == ["ls-remote"]:
    if state.get("ls_remote_fails"):
        sys.exit(2)
    sys.stdout.write(state["remote_tag"])
elif args[:2] == ["merge-base", "--is-ancestor"]:
    sys.exit(0 if state["on_main"] and args[2] == state["tag_commit"] and args[3] == "origin/main" else 1)
elif args[:1] == ["archive"]:
    prefix = [a for a in args if a.startswith("--prefix=")][0].split("=", 1)[1]
    out = args[args.index("-o") + 1]
    with tarfile.open(out, "w:gz") as tar:
        data = b"#!/bin/sh\n"
        info = tarfile.TarInfo(prefix + "bin/moshi-cockpit")
        info.size = len(data)
        tar.addfile(info, io.BytesIO(data))
else:
    sys.stderr.write("fake git: unexpected %r\n" % (args,))
    sys.exit(2)
'''

FAKE_GH = r'''#!/bin/sh
printf '%s\n' "$PWD" "$@" > "$FAKE_GH_CALL"
'''


class ReleaseTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tmp = Path(os.path.realpath(tmp.name))
        self.repo = self.tmp / "repo"
        (self.repo / "tools").mkdir(parents=True)
        (self.repo / "cockpit").mkdir()
        for name in ("release.sh", "build_release.py", "leak_check.py"):
            shutil.copy2(ROOT / "tools" / name, self.repo / "tools" / name)
        shutil.copy2(ROOT / "install.sh", self.repo / "install.sh")
        (self.repo / "cockpit" / "__init__.py").write_text(f'__version__ = "{VERSION}"\n')
        self.set_check(0)
        self.fakes = self.tmp / "fakes"
        self.fakes.mkdir()
        for name, text in (("git", FAKE_GIT), ("gh", FAKE_GH)):
            (self.fakes / name).write_text(text)
            (self.fakes / name).chmod(0o755)
        self.words = self.tmp / "words.txt"
        self.words.write_text(WORD + "\n")
        # origin holds the same annotated tag: the tag object, then the commit it peels to
        self.state = {"status": "", "tag": TAG, "tag_commit": COMMIT, "head": COMMIT, "on_main": True,
                      "remote_tag": f"{'9' * 40}\trefs/tags/{TAG}\n{COMMIT}\trefs/tags/{TAG}^{{}}\n"}
        self.log = self.tmp / "git.log"
        self.gh_call = self.tmp / "gh-call"

    def set_check(self, code: int) -> None:
        (self.repo / "tools" / "check.sh").write_text(f"#!/bin/sh\necho checks ran\nexit {code}\n")

    def env(self):
        state = self.tmp / "state.json"
        state.write_text(json.dumps(self.state))
        return {"PATH": f"{self.fakes}:{SYSTEM_PATH}", "HOME": str(self.tmp / "home"), "FAKE_STATE": str(state),
                "FAKE_LOG": str(self.log), "FAKE_GH_CALL": str(self.gh_call),
                "MOSHI_COCKPIT_BLOCKED_WORDS": str(self.words)}

    def release(self, *args: str) -> subprocess.CompletedProcess:
        return subprocess.run(["sh", str(self.repo / "tools" / "release.sh"), *args], capture_output=True,
                              text=True, env=self.env(), stdin=subprocess.DEVNULL, timeout=60)

    def release_on_terminal(self, answer: bytes, *args: str):
        """stdin is a pty (so `[ -t 0 ]` holds) that already holds the answer."""
        master, slave = os.openpty()
        drained = threading.Thread(target=self._drain, args=(master,), daemon=True)
        drained.start()
        os.write(master, answer)
        try:
            done = subprocess.run(["sh", str(self.repo / "tools" / "release.sh"), *args], capture_output=True,
                                  text=True, env=self.env(), stdin=slave, timeout=60)
        finally:
            os.close(slave)
            drained.join(timeout=5)
            os.close(master)
        return done

    @staticmethod
    def _drain(master: int) -> None:
        while True:
            try:
                if not os.read(master, 4096):
                    return
            except OSError:
                return

    def git_calls(self) -> List[List[str]]:
        if not self.log.exists():
            return []
        return [json.loads(line) for line in self.log.read_text().splitlines()]

    @property
    def dist(self) -> Path:
        return self.repo / "dist" / TAG

    def assert_refused(self, done: subprocess.CompletedProcess, text: str) -> None:
        self.assertEqual(done.returncode, 1, done.stdout + done.stderr)
        self.assertIn(text, done.stderr)
        self.assertFalse(self.gh_call.exists())

    def test_publishes_after_a_yes_on_a_terminal(self):
        done = self.release_on_terminal(b"y\n", TAG)
        self.assertEqual(done.returncode, 0, done.stdout + done.stderr)
        self.assertIn(f"Publish {TAG}? [y/N]", done.stdout)
        call = self.gh_call.read_text().splitlines()
        self.assertEqual(call[0], str(self.dist))
        self.assertEqual(call[1:], ["release", "create", TAG, "install.sh", f"moshi-cockpit-{VERSION}.tar.gz",
                                    "SHA256SUMS", "--verify-tag", "--notes-file", "notes.md"])
        payload = (self.dist / f"moshi-cockpit-{VERSION}.tar.gz").read_bytes()
        payload_sha = hashlib.sha256(payload).hexdigest()
        install = (self.dist / "install.sh").read_text()
        self.assertIn(f"PAYLOAD_SHA256='{payload_sha}'", install)
        self.assertIn(f"VERSION='{VERSION}'", install)
        sums = (self.dist / "SHA256SUMS").read_text()
        self.assertIn(f"{payload_sha}  moshi-cockpit-{VERSION}.tar.gz\n", sums)
        self.assertIn(f"{hashlib.sha256(install.encode()).hexdigest()}  install.sh\n", sums)
        notes = (self.dist / "notes.md").read_text()
        self.assertIn("curl -fsSL https://github.com/pcas-io/moshi-cockpit/releases/latest/download/install.sh | sh",
                      notes)
        self.assertIn("Linux machine (x86_64 or aarch64, headless)", notes)
        self.assertIn(sums.strip(), notes)
        archive = [c for c in self.git_calls() if c[1] == "archive"][0]
        self.assertEqual(archive[-5:], [TAG, "cockpit", "bin", "share", "LICENSE"])
        self.assertIn(["git", "ls-remote", "origin", f"refs/tags/{TAG}", f"refs/tags/{TAG}^{{}}"], self.git_calls())
        self.assertIn(f"--prefix=moshi-cockpit-{VERSION}/", archive)
        with tarfile.open(self.dist / f"moshi-cockpit-{VERSION}.tar.gz") as tar:
            self.assertEqual(tar.getnames(), [f"moshi-cockpit-{VERSION}/bin/moshi-cockpit"])

    def test_the_default_answer_is_no(self):
        done = self.release_on_terminal(b"\n", TAG)
        self.assertEqual(done.returncode, 1)
        self.assertIn("Not published", done.stdout + done.stderr)
        self.assertFalse(self.gh_call.exists())

    def test_without_a_terminal_it_builds_but_never_publishes(self):
        done = self.release(TAG)
        self.assertEqual(done.returncode, 1)
        self.assertIn("Not published", done.stderr)
        self.assertTrue((self.dist / "SHA256SUMS").is_file())
        self.assertFalse(self.gh_call.exists())

    def test_extra_notes_go_into_the_release_notes(self):
        extra = self.tmp / "extra.md"
        extra.write_text("Fixes the sidebar.\n")
        done = self.release_on_terminal(b"y\n", TAG, str(extra))
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertIn("Fixes the sidebar.", (self.dist / "notes.md").read_text())

    def test_a_blocked_word_in_the_notes_stops_the_release(self):
        extra = self.tmp / "extra.md"
        extra.write_text(f"Thanks to {WORD}.\n")
        done = self.release_on_terminal(b"y\n", TAG, str(extra))
        self.assert_refused(done, "notes")

    def test_refuses_a_dirty_tree(self):
        self.state["status"] = " M cockpit/cli.py\n"
        self.assert_refused(self.release(TAG), "working tree is not clean")

    def test_refuses_a_missing_tag(self):
        self.state["tag"] = None
        self.assert_refused(self.release(TAG), f"tag {TAG} does not exist")

    def test_refuses_when_head_is_not_the_tag(self):
        self.state["head"] = OTHER
        self.assert_refused(self.release(TAG), f"check out {TAG} first")

    def test_refuses_a_tag_that_is_not_on_origin_main(self):
        self.state["on_main"] = False
        done = self.release(TAG)
        self.assert_refused(done, "not on origin/main")
        self.assertIn(["git", "fetch", "-q", "origin", "main"], self.git_calls())

    def test_refuses_a_version_that_differs_from_the_package(self):
        (self.repo / "cockpit" / "__init__.py").write_text('__version__ = "1.2.2"\n')
        self.assert_refused(self.release(TAG), "__version__")

    def test_refuses_when_the_checks_fail(self):
        self.set_check(1)
        done = self.release(TAG)
        self.assert_refused(done, "tools/check.sh failed")
        self.assertFalse(self.dist.exists())

    def test_a_lightweight_tag_on_origin_counts_by_its_commit(self):
        self.state["remote_tag"] = f"{COMMIT}\trefs/tags/{TAG}\n"
        done = self.release_on_terminal(b"y\n", TAG)
        self.assertEqual(done.returncode, 0, done.stdout + done.stderr)
        self.assertTrue(self.gh_call.exists())

    def test_refuses_when_origin_has_no_such_tag(self):
        self.state["remote_tag"] = ""
        self.assert_refused(self.release_on_terminal(b"y\n", TAG), f"git push origin {TAG}")

    def test_refuses_when_the_tag_on_origin_points_elsewhere(self):
        for remote in (f"{OTHER}\trefs/tags/{TAG}\n",
                       f"{COMMIT}\trefs/tags/{TAG}\n{OTHER}\trefs/tags/{TAG}^{{}}\n"):
            with self.subTest(remote=remote):
                self.state["remote_tag"] = remote
                self.assert_refused(self.release_on_terminal(b"y\n", TAG), "on origin")

    def test_refuses_when_origin_cannot_be_asked_for_the_tag(self):
        self.state["ls_remote_fails"] = True
        self.assert_refused(self.release_on_terminal(b"y\n", TAG), "could not ask origin")

    def test_refuses_a_malformed_version(self):
        for bad in ("1.2.3", "v1.2", "v1.2.3-rc1", ""):
            with self.subTest(bad=bad):
                self.assert_refused(self.release(bad), "usage")
        self.assertEqual(self.git_calls(), [])


if __name__ == "__main__":
    unittest.main()
