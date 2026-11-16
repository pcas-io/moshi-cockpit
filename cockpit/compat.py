"""The Herdr versions this release was tested with, and what doctor says about the installed ones.

Herdr comes from its vendor (https://herdr.dev) and updates itself. Nothing here downloads, pins or runs it:
the versions are read from the bundles' Info.plist, and a version that was not tested is a warning, never an
error.
"""

from __future__ import annotations

import os
import plistlib
import unicodedata
from dataclasses import dataclass
from pathlib import Path
from typing import List, Optional, Sequence, Tuple

from cockpit.paths import Paths

TESTED_HERDR_APP = ("0.1.13",)
TESTED_HERDR_CORE = ("0.1.18",)
HERDR_URL = "https://herdr.dev"
UNKNOWN = "unknown"  # the bundle is there, its version cannot be read
MAX_VERSION = 40

OK, WARN, FAIL = "ok", "warn", "fail"


@dataclass(frozen=True)
class Finding:
    name: str
    status: str
    detail: str


def check(app_version: Optional[str], core_version: Optional[str]) -> List[Finding]:
    """Herdr.app must be there (None: missing); any version of it or of herdr-core that was not tested, an
    unusual or an unreadable one included, only warns."""
    if not app_version:
        return [Finding("herdr", FAIL, f"Herdr is not installed. Install it from {HERDR_URL}")]
    findings = [_judge("herdr", "Herdr", app_version, TESTED_HERDR_APP)]
    if core_version:
        findings.append(_judge("herdr-core", "herdr-core", core_version, TESTED_HERDR_CORE))
    return findings


def _judge(name: str, label: str, version: str, tested: Sequence[str]) -> Finding:
    if version in tested:
        return Finding(name, OK, f"{label} {version} (tested)")
    if version == UNKNOWN:
        return Finding(name, WARN, f"{label} is installed, but its version is unknown (tested: {', '.join(tested)}). "
                                   "It usually works; if something breaks, say which version you run")
    return Finding(name, WARN, f"{label} {version} was not tested with this release (tested: {', '.join(tested)}). "
                               "It usually works; if something breaks, say which version you run")


def installed_versions(paths: Paths) -> Tuple[Optional[str], Optional[str]]:
    """(Herdr.app, herdr-core) as their bundles state them: None where a bundle is missing, UNKNOWN where it
    is there but its version cannot be read."""
    core = core_bundle(paths)
    return (bundle_version(paths.herdr_app_bundle),
            bundle_version(core) if core else None)


def core_bundle(paths: Paths) -> Optional[Path]:
    """The 'Herdr Core.app' behind ~/.local/bin/herdr-core, if any. Nothing is executed."""
    binary = paths.herdr_core_bin
    if not binary.exists():
        return None
    bundle = Path(os.path.realpath(binary)).parent.parent.parent  # .../Herdr Core.app/Contents/MacOS/herdr-core
    return bundle if bundle.suffix == ".app" else None


def bundle_version(bundle: Path) -> Optional[str]:
    """The bundle's CFBundleShortVersionString, without control characters and bounded, read without following a
    link at Info.plist itself. None only when the bundle itself is missing."""
    if not bundle.is_dir():
        return None
    info = bundle / "Contents" / "Info.plist"
    if info.is_symlink() or not info.is_file():
        return UNKNOWN
    try:
        fd = os.open(info, os.O_RDONLY | os.O_NOFOLLOW)
        with os.fdopen(fd, "rb") as handle:
            data = plistlib.load(handle)
    except (OSError, ValueError, plistlib.InvalidFileException):
        return UNKNOWN
    version = data.get("CFBundleShortVersionString") if isinstance(data, dict) else None
    if not isinstance(version, str):
        return UNKNOWN
    shown = "".join(ch for ch in version if not unicodedata.category(ch).startswith("C")).strip()[:MAX_VERSION]
    return shown or UNKNOWN
