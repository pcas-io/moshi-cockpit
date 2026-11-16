"""Detect and install herdr-core and herdr-app from pinned, verified upstream packages."""

from __future__ import annotations

import os
import plistlib
import shutil
import subprocess
import tarfile
import tempfile
import urllib.request
from pathlib import Path, PurePosixPath
from typing import Callable, Optional, Tuple

from cockpit.paths import USER_AGENT, Paths
from cockpit.pins import Pin, PinError, parse_version, verify_codesign, verify_sha256

Runner = Callable[..., "subprocess.CompletedProcess[str]"]
MAX_DOWNLOAD = 200 * 1024 * 1024


def core_bundle(paths: Paths) -> Optional[Path]:
    """The installed 'Herdr Core.app' behind ~/.local/bin/herdr-core, if any. Nothing is executed."""
    binary = paths.herdr_core_bin
    if not binary.exists():
        return None
    real = Path(os.path.realpath(binary))
    # .../Herdr Core.app/Contents/MacOS/herdr-core
    bundle = real.parent.parent.parent
    return bundle if bundle.suffix == ".app" and (bundle / "Contents" / "Info.plist").is_file() else None


def installed_core_version(paths: Paths) -> Optional[Tuple[int, ...]]:
    """Read from the bundle's Info.plist: an installed binary is never run before its signature is checked."""
    bundle = core_bundle(paths)
    return _plist_version(bundle / "Contents" / "Info.plist") if bundle else None


def installed_app_version(paths: Paths) -> Optional[Tuple[int, ...]]:
    return _plist_version(paths.herdr_app_bundle / "Contents" / "Info.plist")


def _plist_version(info: Path) -> Optional[Tuple[int, ...]]:
    """The bundle's version without following a link at Info.plist itself; a broken plist is 'unknown'."""
    if info.is_symlink() or not info.is_file():
        return None
    try:
        fd = os.open(info, os.O_RDONLY | os.O_NOFOLLOW)
        with os.fdopen(fd, "rb") as handle:
            data = plistlib.load(handle)
        return parse_version(str(data.get("CFBundleShortVersionString", "")))
    except (OSError, ValueError, plistlib.InvalidFileException, AttributeError):
        return None


def installed_bundle(component: str, paths: Paths) -> Optional[Path]:
    if component == "herdr-core":
        return core_bundle(paths)
    return paths.herdr_app_bundle if paths.herdr_app_bundle.is_dir() else None


def drift(installed: Optional[Tuple[int, ...]], pin: Pin) -> str:
    """'missing', 'equal', 'newer' or 'older' relative to the pin."""
    if installed is None:
        return "missing"
    pinned = parse_version(pin.version)
    if installed == pinned:
        return "equal"
    return "newer" if installed > pinned else "older"


def download(url: str, dest: Path, opener: Callable = urllib.request.urlopen) -> None:
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with opener(request, timeout=60) as response, dest.open("wb") as out:
        total = 0
        while True:
            chunk = response.read(1 << 20)
            if not chunk:
                break
            total += len(chunk)
            if total > MAX_DOWNLOAD:
                raise PinError("Download ist größer als erwartet, abgebrochen")
            out.write(chunk)


MAX_EXTRACTED = 1024 * 1024 * 1024


def _inside(path: Path, root: Path) -> bool:
    """Component-wise, not a string prefix: /x/out2 is not inside /x/out."""
    try:
        path.relative_to(root)
        return True
    except ValueError:
        return False


def _check_member(member: tarfile.TarInfo) -> None:
    name = PurePosixPath(member.name)
    if name.is_absolute() or ".." in name.parts or not name.parts:
        raise PinError(f"Archiv enthält unsicheren Pfad: {member.name}")
    if not (member.isfile() or member.isdir() or member.issym()):
        # Hard links, devices, FIFOs: an app bundle needs none of them.
        raise PinError(f"Archiv enthält nicht erlaubten Eintrag ({member.type!r}): {member.name}")
    if member.issym():
        link = PurePosixPath(member.linkname)
        if link.is_absolute() or not member.linkname:
            raise PinError(f"Archiv enthält unsicheren Link: {member.name}")


def safe_extract(archive: Path, dest: Path) -> None:
    """Extracts member by member, our own code on every Python (no extractall, no fallback).

    Before anything is written all members are checked: relative names without '..', only files,
    folders and relative symlinks. While writing, the real path (all links resolved) of every parent
    and of every new symlink must stay inside the target; files are created exclusively, so nothing
    is written through a link that an earlier member put in place.
    """
    root = dest.resolve()
    with tarfile.open(archive, "r:gz") as tar:
        members = tar.getmembers()
        for member in members:
            _check_member(member)
        if sum(m.size for m in members if m.isfile()) > MAX_EXTRACTED:
            raise PinError("Archiv ist entpackt größer als erwartet, abgebrochen")
        for member in members:
            target = root / member.name
            ancestor = target.parent
            while not ancestor.exists():
                ancestor = ancestor.parent
            if not _inside(Path(os.path.realpath(ancestor)), root):
                raise PinError(f"Archiv schreibt über einen Link nach außen: {member.name}")
            target.parent.mkdir(parents=True, exist_ok=True)
            if member.isdir():
                target.mkdir(mode=0o755, exist_ok=True)
            elif member.issym():
                try:
                    os.symlink(member.linkname, target)
                except FileExistsError:
                    raise PinError(f"Archiv enthält einen Eintrag doppelt: {member.name}") from None
                if not _inside(Path(os.path.realpath(target)), root):
                    target.unlink()
                    raise PinError(f"Archiv enthält unsicheren Link: {member.name}")
            else:
                source = tar.extractfile(member)
                if source is None:
                    raise PinError(f"Archiv-Eintrag nicht lesbar: {member.name}")
                try:
                    fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
                except FileExistsError:
                    raise PinError(f"Archiv enthält einen Eintrag doppelt: {member.name}") from None
                with source, os.fdopen(fd, "wb") as out:
                    shutil.copyfileobj(source, out)
                os.chmod(target, 0o755 if member.mode & 0o111 else 0o644)
        # A later link can change where an earlier one points (a -> "b/..", then b -> "."): each passed
        # its own check, together a leads outside. So every link is checked again in the finished tree.
        for member in members:
            if member.issym() and not _inside(Path(os.path.realpath(root / member.name)), root):
                raise PinError(f"Archiv enthält einen Link, der im fertigen Baum nach außen zeigt: {member.name}")


def fetch_verified(pin: Pin, workdir: Path, opener: Callable = urllib.request.urlopen) -> Path:
    archive = workdir / f"{pin.component}.tar.gz"
    download(pin.url, archive, opener)
    verify_sha256(archive, pin)
    extract_dir = workdir / pin.component
    extract_dir.mkdir()
    safe_extract(archive, extract_dir)
    return extract_dir


def install_core(pin: Pin, paths: Paths, runner: Runner = subprocess.run) -> str:
    """Installs the pinned core the way upstream's installer does, after our own checks."""
    with tempfile.TemporaryDirectory(prefix="moshi-cockpit-") as tmp:
        extracted = fetch_verified(pin, Path(tmp))
        bundle = extracted / "Herdr Core.app"
        bundled_bin = bundle / "Contents" / "MacOS" / "herdr-core"
        if not bundled_bin.is_file():
            raise PinError("herdr-core-Paket enthält kein 'Herdr Core.app'")
        verify_codesign(bundle, pin, runner)
        paths.local_bin.mkdir(parents=True, exist_ok=True)
        result = runner(
            [str(bundled_bin), "--hc-install-bundle", str(bundled_bin), str(paths.herdr_core_bin)],
            capture_output=True, text=True, check=False, timeout=120,
        )
        if result.returncode != 0:
            raise PinError("Installation von Herdr Core.app fehlgeschlagen")
    hc_link = paths.local_bin / "hc"
    if not hc_link.exists() and not hc_link.is_symlink():
        hc_link.symlink_to("herdr-core")
    return f"herdr-core {pin.version} installiert (Prüfsumme + Codesignatur geprüft)"


def install_app(pin: Pin, paths: Paths, runner: Runner = subprocess.run) -> str:
    with tempfile.TemporaryDirectory(prefix="moshi-cockpit-") as tmp:
        extracted = fetch_verified(pin, Path(tmp))
        bundle = extracted / "Herdr.app"
        if not bundle.is_dir():
            raise PinError("herdr-app-Paket enthält kein 'Herdr.app'")
        verify_codesign(bundle, pin, runner)
        if paths.herdr_app_bundle.exists():
            raise PinError(f"{paths.herdr_app_bundle} existiert bereits, kein Überschreiben")
        # ditto keeps extended attributes and the signature intact; shutil.copytree would not.
        result = runner(["/usr/bin/ditto", str(bundle), str(paths.herdr_app_bundle)],
                        capture_output=True, text=True, check=False, timeout=120)
        if result.returncode != 0:
            shutil.rmtree(paths.herdr_app_bundle, ignore_errors=True)
            raise PinError("Kopieren von Herdr.app nach /Applications fehlgeschlagen")
    return f"Herdr.app {pin.version} installiert (Prüfsumme + Codesignatur geprüft)"
