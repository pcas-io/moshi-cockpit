"""Where secrets live: one small interface, one implementation per platform.

macOS uses the Keychain, a Linux machine the Secret Service or a private file (see linux.py). Callers ask
`default_store()` and never name a platform.
"""

from __future__ import annotations

import os
import platform
import re
import shutil
import subprocess
from pathlib import Path
from typing import Callable, Optional, Protocol, runtime_checkable

# Tokens from ocellio and moshi are URL-safe. A strict charset keeps the
# `security -i` command line injection-free without shell quoting tricks.
_TOKEN_RE = re.compile(r"^[A-Za-z0-9._~\-]{8,512}$")


class KeychainError(RuntimeError):
    """A secret store failed. The message names the entry, never its value."""


class UnsupportedPlatform(RuntimeError):
    """There is no secret store for this operating system yet."""


class SecretStore(Protocol):
    name: str  # human-readable, for doctor and prompts

    def exists(self, service: str, account: str) -> bool: ...

    def read(self, service: str, account: str) -> str: ...

    def write(self, service: str, account: str, secret: str, strict: bool = False) -> None: ...

    def delete(self, service: str, account: str) -> None: ...


@runtime_checkable
class HardenedStore(Protocol):
    """Optional extras of a store that can harden entries (macOS: an empty trusted-app list). Callers check
    `isinstance(store, HardenedStore)` before they use them; a plain SecretStore simply has none."""

    def is_strict(self, service: str, account: str) -> bool: ...

    def has_parked(self, service: str, account: str) -> bool: ...

    def restore_parked(self, service: str, account: str) -> bool: ...


def validate_token(secret: str) -> None:
    if not _TOKEN_RE.match(secret or ""):
        raise KeychainError("Unexpected token format (allowed: A-Z a-z 0-9 . _ ~ -, 8 to 512 characters)")


def check_names(service: str, account: str) -> None:
    for part in (service, account):
        if not re.match(r"^[A-Za-z0-9._\-]{1,64}$", part):
            raise KeychainError("Invalid service or account name")


def default_store(system: Callable[[], str] = platform.system, home: Optional[Path] = None,
                  runner=subprocess.run, which=shutil.which) -> SecretStore:
    name = system()
    if name == "Darwin":
        from cockpit.secrets.macos import MacKeychain
        return MacKeychain()
    if name == "Linux":
        from cockpit.secrets import linux
        return linux.select_store(home or Path(os.environ.get("HOME") or Path.home()), runner, which)
    raise UnsupportedPlatform("Windows is not supported yet")
