"""Sanitizing untrusted text (server responses, other agents' fields) before it reaches the terminal."""

from __future__ import annotations

import unicodedata


def clean(value: object, fallback: str = "–", limit: int = 0) -> str:
    """Replaces control characters (ANSI/OSC escapes, CR/LF, BEL) with spaces; optional length limit."""
    text = str(value) if value not in (None, "") else fallback
    text = "".join(" " if unicodedata.category(ch).startswith("C") else ch for ch in text)
    return text[:limit] if limit else text
