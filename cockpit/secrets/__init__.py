"""Where secrets live: one small interface, one implementation per platform.

Only macOS (Keychain) exists today. Callers ask `default_store()` and never name a platform.
"""

from __future__ import annotations

import platform
import re
from typing import Protocol

# Tokens from ocellio and moshi are URL-safe. A strict charset keeps the
# `security -i` command line injection-free without shell quoting tricks.
_TOKEN_RE = re.compile(r"^[A-Za-z0-9._~\-]{8,512}$")


class KeychainError(RuntimeError):
    """A secret store failed. The message names the entry, never its value."""


class UnsupportedPlatform(RuntimeError):
    """There is no secret store for this operating system yet."""


class SecretStore(Protocol):
    def exists(self, service: str, account: str) -> bool: ...

    def read(self, service: str, account: str) -> str: ...

    def write(self, service: str, account: str, secret: str, strict: bool = False) -> None: ...

    def delete(self, service: str, account: str) -> None: ...


def validate_token(secret: str) -> None:
    if not _TOKEN_RE.match(secret or ""):
        raise KeychainError("Unexpected token format (allowed: A-Z a-z 0-9 . _ ~ -, 8 to 512 characters)")


def default_store() -> SecretStore:
    if platform.system() == "Darwin":
        from cockpit.secrets.macos import MacKeychain
        return MacKeychain()
    raise UnsupportedPlatform("Windows and Linux are not supported yet")
