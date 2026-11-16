import copy
import hashlib
import io
import json
import os
import plistlib
import shutil
import subprocess
import tarfile
import tempfile
import unittest
from pathlib import Path

from cockpit import herdr
from cockpit.pins import PINNED, Pin, PinError, codesign_requirement, load_pins, parse_version, verify_codesign, verify_sha256
from tests.helpers import temp_paths


def make_pin(sha: str) -> Pin:
    return Pin("herdr-core", "0.1.18", "dev.herdr.core", "https://releases.herdr.dev/core/x.tar.gz", sha, "2W9652Q7QR")


class PinsTest(unittest.TestCase):
    def test_built_in_pins_load(self):
        pins = load_pins()
        self.assertEqual(set(pins), {"herdr-core", "herdr-app"})
        for pin in pins.values():
            self.assertEqual(len(pin.sha256), 64)
            self.assertEqual(pin.team_id, "2W9652Q7QR")

    def test_rejects_foreign_download_host(self):
        with tempfile.TemporaryDirectory() as tmp:
            data = copy.deepcopy(PINNED)
            data["herdr-app"]["targets"]["macos-aarch64"]["url"] = "https://evil.example/app.tgz"
            path = Path(tmp) / "versions.json"
            path.write_text(json.dumps(data))
            with self.assertRaises(PinError):
                load_pins(path)

    def test_parse_version(self):
        self.assertEqual(parse_version("hc 0.1.18"), (0, 1, 18))
        self.assertEqual(parse_version("0.1.13"), (0, 1, 13))
        with self.assertRaises(ValueError):
            parse_version("unknown")

    def test_sha256_match_and_mismatch(self):
        with tempfile.TemporaryDirectory() as tmp:
            file = Path(tmp) / "pkg.tgz"
            file.write_bytes(b"payload")
            verify_sha256(file, make_pin(hashlib.sha256(b"payload").hexdigest()))
            with self.assertRaises(PinError):
                verify_sha256(file, make_pin("0" * 64))

    def test_codesign_requirement_pins_identifier_and_team(self):
        requirement = codesign_requirement(make_pin("0" * 64))
        self.assertIn('identifier "dev.herdr.core"', requirement)
        self.assertIn('subject.OU] = "2W9652Q7QR"', requirement)

    def test_codesign_failure_raises(self):
        def runner(args, **_):
            return subprocess.CompletedProcess(args, 3, "", "requirement not satisfied")
        with self.assertRaises(PinError):
            verify_codesign(Path("/tmp/x.app"), make_pin("0" * 64), runner)


class HerdrTest(unittest.TestCase):
    def test_drift(self):
        pin = make_pin("0" * 64)
        self.assertEqual(herdr.drift(None, pin), "missing")
        self.assertEqual(herdr.drift((0, 1, 18), pin), "equal")
        self.assertEqual(herdr.drift((0, 1, 19), pin), "newer")
        self.assertEqual(herdr.drift((0, 1, 2), pin), "older")

    def test_app_version_from_info_plist(self):
        paths = temp_paths(self)
        contents = paths.herdr_app_bundle / "Contents"
        contents.mkdir(parents=True)
        import plistlib
        with (contents / "Info.plist").open("wb") as handle:
            plistlib.dump({"CFBundleShortVersionString": "0.1.13"}, handle)
        self.assertEqual(herdr.installed_app_version(paths), (0, 1, 13))

    def _archive(self, tmp: Path, name: str, linkname: str = "") -> Path:
        archive = tmp / "evil.tgz"
        with tarfile.open(archive, "w:gz") as tar:
            info = tarfile.TarInfo(name)
            if linkname:
                info.type, info.linkname = tarfile.SYMTYPE, linkname
                tar.addfile(info)
            else:
                info.size = 1
                tar.addfile(info, io.BytesIO(b"x"))
        return archive

    def test_safe_extract_rejects_path_traversal(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            (tmp / "out").mkdir()
            with self.assertRaises(PinError):
                herdr.safe_extract(self._archive(tmp, "../escape.txt"), tmp / "out")

    def test_safe_extract_rejects_escaping_symlink(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            (tmp / "out").mkdir()
            with self.assertRaises(PinError):
                herdr.safe_extract(self._archive(tmp, "link", linkname="/etc/passwd"), tmp / "out")

    @staticmethod
    def _tar(tmp, entries):
        """entries: (name, kind, data_or_linkname) with kind file/dir/sym/hard."""
        archive = tmp / "t.tgz"
        with tarfile.open(archive, "w:gz") as tar:
            for name, kind, extra in entries:
                info = tarfile.TarInfo(name)
                if kind == "dir":
                    info.type = tarfile.DIRTYPE
                    tar.addfile(info)
                elif kind in ("sym", "hard"):
                    info.type, info.linkname = (tarfile.SYMTYPE if kind == "sym" else tarfile.LNKTYPE), extra
                    tar.addfile(info)
                else:
                    info.size, info.mode = len(extra), 0o755
                    tar.addfile(info, io.BytesIO(extra))
        return archive

    def _extract(self, entries):
        tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, tmp, True)
        out = tmp / "out"
        out.mkdir()
        herdr.safe_extract(self._tar(tmp, entries), out)
        return tmp, out

    def test_safe_extract_keeps_a_bundle_with_internal_links(self):
        _, out = self._extract([("X.app/Contents/Frameworks/F/Versions/A/F", "file", b"bin"),
                                ("X.app/Contents/Frameworks/F/Versions/Current", "sym", "A"),
                                ("X.app/Contents/Frameworks/F/F", "sym", "Versions/Current/F")])
        self.assertEqual((out / "X.app/Contents/Frameworks/F/F").read_bytes(), b"bin")
        self.assertTrue(os.access(out / "X.app/Contents/Frameworks/F/Versions/A/F", os.X_OK))

    def test_safe_extract_rejects_hardlinks(self):
        with self.assertRaises(PinError):
            self._extract([("a", "file", b"x"), ("b", "hard", "a")])

    def test_safe_extract_rejects_a_link_chain_that_resolves_outside(self):
        # Lexically "a/b/S/../.." is "a", inside; really S is the root, so it ends one level above it.
        with self.assertRaises(PinError):
            self._extract([("a/b/S", "sym", "../.."), ("a/b/A", "sym", "S/../..")])

    def test_safe_extract_rejects_links_that_only_escape_together(self):
        # a -> "b/.." is the root while b is missing; b -> "." then makes a the root's parent.
        with self.assertRaises(PinError) as ctx:
            self._extract([("a", "sym", "b/.."), ("b", "sym", ".")])
        self.assertIn("fertigen Baum", str(ctx.exception))

    def test_safe_extract_never_writes_through_a_link(self):
        tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, tmp, True)
        (tmp / "victim").mkdir()
        out = tmp / "out"
        out.mkdir()
        for entries in ([("up", "sym", ".."), ("up/victim/x", "file", b"pwned")],
                        [("x", "file", b"one"), ("x", "file", b"two")],
                        [("x", "file", b"one"), ("x", "sym", "y")]):
            with self.subTest(entries=entries), self.assertRaises(PinError):
                herdr.safe_extract(self._tar(tmp, entries), out)
        self.assertEqual(list((tmp / "victim").iterdir()), [])

    def test_safe_extract_prefix_sibling_is_not_inside(self):
        self.assertFalse(herdr._inside(Path("/x/out2/a"), Path("/x/out")))
        self.assertTrue(herdr._inside(Path("/x/out/a"), Path("/x/out")))

    def test_core_version_comes_from_the_plist_without_running_anything(self):
        paths = temp_paths(self)
        app = paths.home / ".local/share/herdr-core/active/Herdr Core.app"
        (app / "Contents/MacOS").mkdir(parents=True)
        (app / "Contents/MacOS/herdr-core").write_text("#!/bin/sh\nexit 99\n")
        with (app / "Contents/Info.plist").open("wb") as handle:
            plistlib.dump({"CFBundleShortVersionString": "0.1.18"}, handle)
        paths.local_bin.mkdir(parents=True)
        (paths.local_bin / "herdr-core").symlink_to(app / "Contents/MacOS/herdr-core")
        self.assertEqual(herdr.installed_core_version(paths), (0, 1, 18))
        self.assertEqual(herdr.installed_bundle("herdr-core", paths), app.resolve())

    def test_broken_or_linked_plist_is_an_unknown_version(self):
        paths = temp_paths(self)
        info = paths.herdr_app_bundle / "Contents" / "Info.plist"
        info.parent.mkdir(parents=True)
        info.write_bytes(b"not a plist")
        self.assertIsNone(herdr.installed_app_version(paths))
        info.unlink()
        other = paths.home / "elsewhere.plist"
        with other.open("wb") as handle:
            plistlib.dump({"CFBundleShortVersionString": "0.1.13"}, handle)
        info.symlink_to(other)
        self.assertIsNone(herdr.installed_app_version(paths))

    def test_fetch_verified_rejects_checksum_mismatch(self):
        class Response(io.BytesIO):
            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return False

        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(PinError):
                herdr.fetch_verified(make_pin("0" * 64), Path(tmp), opener=lambda *a, **k: Response(b"tampered"))


if __name__ == "__main__":
    unittest.main()
