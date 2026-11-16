"""Pinned herdr versions: load, compare, verify checksum and Apple code signature."""

from __future__ import annotations

import hashlib
import json
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Dict, Optional, Tuple

Runner = Callable[..., "subprocess.CompletedProcess[str]"]
TARGET = "macos-aarch64"

# The herdr builds we install. Downloads are checked against THESE values, never against the manifest of the
# release server. A pin changes only together with its new SHA-256, checked against the upstream manifest.
PINNED: Dict[str, Any] = {
    "codesign_team_id": "2W9652Q7QR",
    "herdr-core": {
        "version": "0.1.18",
        "identifier": "dev.herdr.core",
        "targets": {"macos-aarch64": {
            "url": "https://releases.herdr.dev/core/0.1.18/herdr-core-macos-aarch64.tar.gz",
            "sha256": "77d8af092e25ae650f4424d6dad27514f6916a11d807fb0752858cd27c21e4fc",
        }},
    },
    "herdr-app": {
        "version": "0.1.13",
        "identifier": "dev.herdr.app",
        "targets": {"macos-aarch64": {
            "url": "https://releases.herdr.dev/app/0.1.13/herdr-app-macos-aarch64.tar.gz",
            "sha256": "f5dd0bc81aa9ef61cfbfb6ef9fdbf4fe7f664d4ec676ee0dc88f13cc9759d0d0",
        }},
    },
}


class PinError(RuntimeError):
    """A download or bundle does not match what we pinned."""


@dataclass(frozen=True)
class Pin:
    component: str
    version: str
    identifier: str
    url: str
    sha256: str
    team_id: str


def load_pins(path: Optional[Path] = None, target: str = TARGET) -> Dict[str, Pin]:
    """The built-in pins, or those of a JSON file with the same shape."""
    data = PINNED if path is None else json.loads(path.read_text())
    team = data["codesign_team_id"]
    pins: Dict[str, Pin] = {}
    for component in ("herdr-core", "herdr-app"):
        entry = data[component]
        build = entry["targets"][target]
        sha = build["sha256"].lower()
        if not re.fullmatch(r"[0-9a-f]{64}", sha):
            raise PinError(f"{component}: SHA-256 in versions.json ist ungültig")
        if not build["url"].startswith("https://releases.herdr.dev/"):
            raise PinError(f"{component}: Download-URL zeigt nicht auf releases.herdr.dev")
        pins[component] = Pin(component, entry["version"], entry["identifier"], build["url"], sha, team)
    return pins


def parse_version(text: str) -> Tuple[int, ...]:
    """'hc 0.1.18' or '0.1.13' -> (0, 1, 18). Raises ValueError on garbage."""
    match = re.search(r"(\d+)\.(\d+)\.(\d+)", text or "")
    if not match:
        raise ValueError(f"keine Versionsnummer in {text!r}")
    return tuple(int(part) for part in match.groups())


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def verify_sha256(path: Path, pin: Pin) -> None:
    actual = sha256_file(path)
    if actual != pin.sha256:
        raise PinError(f"{pin.component} {pin.version}: Prüfsumme stimmt nicht mit versions.json überein")


def codesign_requirement(pin: Pin) -> str:
    return (
        f'=identifier "{pin.identifier}" and anchor apple generic '
        f'and certificate leaf[subject.OU] = "{pin.team_id}"'
    )


def verify_codesign(bundle: Path, pin: Pin, runner: Runner = subprocess.run) -> None:
    """Independent of the release server: Apple's chain plus Herdr, Inc.'s team id."""
    result = runner(
        ["/usr/bin/codesign", "--verify", "--strict", "-R", codesign_requirement(pin), str(bundle)],
        capture_output=True, text=True, check=False,
    )
    if result.returncode != 0:
        raise PinError(f"{pin.component}: Codesignatur von {bundle.name} gehört nicht zu Team {pin.team_id}")
