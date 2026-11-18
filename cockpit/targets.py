"""The one rule for anything that reaches hc's or ssh's argv as a machine name, ssh alias or target."""

from __future__ import annotations

import re

NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._@:-]{0,253}$")


def check_target(text: str) -> str:
    """The text unchanged when hc can only read it as a name, else ValueError."""
    if not isinstance(text, str) or not NAME_RE.fullmatch(text):
        raise ValueError(f"'{text}' cannot be passed on to hc")
    return text
