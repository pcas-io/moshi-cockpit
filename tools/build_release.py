#!/usr/bin/env python3
"""Renders the release installer: install.sh (the template in the repo root) with the version, the payload's
SHA-256 and the pinned Pythons. tools/release.sh calls it; the result is the `install.sh` asset of a release.

    python3 tools/build_release.py --version X.Y.Z --payload moshi-cockpit-X.Y.Z.tar.gz --out install.sh

The command line always renders the public URLs over https and /Applications/Herdr.app. `render()` takes other
values only so the tests can run the script against a local server.
"""

from __future__ import annotations

import argparse
import hashlib
import re
import sys
from pathlib import Path
from typing import Dict, List, Optional, Tuple

TEMPLATE = Path(__file__).resolve().parent.parent / "install.sh"
REPO_URL = "https://github.com/pcas-io/moshi-cockpit"
HERDR_APP = "/Applications/Herdr.app"

# Python for the installed cockpit: python-build-standalone by Astral, CPython 3.12.16, install_only, one per
# platform install.sh supports (Linux: the glibc builds). Source:
# https://github.com/astral-sh/python-build-standalone/releases/tag/20261026 (asset digests and the release's
# SHA256SUMS agree), macOS looked up 2026-11-17, Linux 2026-11-18. A new pin needs both values from that page again.
PYTHON_VERSION = "3.12.16"
_ASTRAL = "https://github.com/astral-sh/python-build-standalone/releases/download/20261026/cpython-3.12.16%2B20261026"
PYTHONS: Dict[str, Tuple[str, str]] = {
    "macos-arm64": (f"{_ASTRAL}-aarch64-apple-darwin-install_only.tar.gz",
                    "fbf4afef53c2a6b72bb741bc06d5671a9dfaf87bd84e5aa73ac9b3d0d589f14e"),
    "linux-x86_64": (f"{_ASTRAL}-x86_64-unknown-linux-gnu-install_only.tar.gz",
                     "6a79e922e50006b601f29e688407659da1c2b86ebcd4e9c7254cf234be8b89de"),
    "linux-aarch64": (f"{_ASTRAL}-aarch64-unknown-linux-gnu-install_only.tar.gz",
                      "a945427411dc6851a1393533b74a4a1ac434321ae397f2ec69897a9b4fde032b"),
}

VERSION_RE = re.compile(r"^[0-9]+\.[0-9]+\.[0-9]+$")
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
PLACEHOLDER_RE = re.compile(r"@[A-Z0-9_]+@")
# Values land inside single quotes in the script: nothing that could end the quote or need escaping.
SAFE_VALUE_RE = re.compile(r"^[A-Za-z0-9._~:/%+=,-]+$")


def default_release_url(version: str) -> str:
    return f"{REPO_URL}/releases/download/v{version}"


def render(version: str, payload_sha256: str, *, pythons: Optional[Dict[str, Tuple[str, str]]] = None,
           release_url: Optional[str] = None, herdr_app: str = HERDR_APP, curl_proto: str = "=https") -> str:
    """The installer for one release. `pythons` holds (url, sha256) for exactly the platforms of PYTHONS.
    Raises ValueError for anything that is not a plain version, hash or URL."""
    if not VERSION_RE.match(version):
        raise ValueError(f"not a version (X.Y.Z): {version!r}")
    pythons = PYTHONS if pythons is None else pythons
    if set(pythons) != set(PYTHONS):
        raise ValueError(f"pythons needs exactly {', '.join(sorted(PYTHONS))}, got {', '.join(sorted(pythons))}")
    hashes = [("payload", payload_sha256)] + [(f"python {key}", sha) for key, (_url, sha) in pythons.items()]
    for name, value in hashes:
        if not SHA256_RE.match(value):
            raise ValueError(f"not a SHA-256 ({name}): {value!r}")
    values = {
        "VERSION": version,
        "PAYLOAD_SHA256": payload_sha256,
        "PYTHON_VERSION": PYTHON_VERSION,
        "RELEASE_URL": release_url or default_release_url(version),
        "HERDR_APP": herdr_app,
        "CURL_PROTO": curl_proto,
    }
    for key, (url, sha) in pythons.items():
        name = key.upper().replace("-", "_")
        values[f"PYTHON_URL_{name}"] = url
        values[f"PYTHON_SHA256_{name}"] = sha
    for name, value in values.items():
        if not SAFE_VALUE_RE.match(value):
            raise ValueError(f"{name} holds characters the script cannot quote: {value!r}")
    script = TEMPLATE.read_text()
    for name, value in values.items():
        script = script.replace(f"@{name}@", value)
    left = PLACEHOLDER_RE.findall(script)
    if left:
        raise ValueError(f"template placeholders without a value: {', '.join(sorted(set(left)))}")
    return script


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="Render the release installer.")
    parser.add_argument("--version", required=True, help="X.Y.Z, without the v")
    parser.add_argument("--payload", required=True, type=Path, help="moshi-cockpit-X.Y.Z.tar.gz")
    parser.add_argument("--out", required=True, type=Path, help="where install.sh goes")
    args = parser.parse_args(argv)
    try:
        script = render(args.version, sha256_file(args.payload))
    except (OSError, ValueError) as exc:
        print(f"build_release: {exc}", file=sys.stderr)
        return 1
    args.out.write_text(script)
    args.out.chmod(0o755)
    return 0


if __name__ == "__main__":
    sys.exit(main())
