"""moshi-cockpit's own settings: which servers exist.

~/.config/moshi-cockpit/config.json:
    {"servers": {"moshi": "https://…/mcp", "ocellio": "https://…/mcp/"}}

Both servers are optional. Without moshi there is no mesh (no presence, inbox, registration); without
ocellio no knowledge graph. Agents can still be created, started and resumed locally. Nothing about a
particular customer is built in: URLs come only from here (set by `install --moshi/--ocellio`).
"""

from __future__ import annotations

import json
import os
import re
import stat
from dataclasses import dataclass, field
from typing import Dict, Optional
from urllib.parse import urlsplit

from cockpit.backup import BackupStore
from cockpit.paths import Paths

SERVERS = ("moshi", "ocellio")
_UNSAFE_CHARS = re.compile(r"[\s\x00-\x1f\x7f]")



def validate_url(url: str) -> str:
    """Parsed with urllib, not a pattern: https, a host, an optional port and path; no user info,
    query or fragment. No allowlist (customers host internally): tokens are bound to the origin."""
    url = (url or "").strip()
    try:
        parts = urlsplit(url)
        port = parts.port  # raises for a malformed port
    except ValueError:
        parts, port = None, None
    if (parts is None or _UNSAFE_CHARS.search(url) or parts.scheme != "https" or not parts.hostname
            or parts.username is not None or parts.password is not None or "@" in parts.netloc
            or parts.query or parts.fragment or "?" in url or "#" in url):
        raise ValueError(f"Ungültige Server-URL '{url[:80]}' (erlaubt: https://host[:port]/pfad, ohne "
                         "Zugangsdaten, ? oder #)")
    return url


def origin(url: str) -> str:
    """scheme://host[:port], the identity a token is bound to."""
    parts = urlsplit(url)
    host = (parts.hostname or "").lower()
    port = parts.port
    if port == {"https": 443, "http": 80}.get(parts.scheme):
        port = None  # https://h and https://h:443 are the same origin
    return f"{parts.scheme}://{host}" + (f":{port}" if port else "")


def read_private(path, what: str) -> str:
    """Config files steer where tokens go: owner-only, no group/other bits, a regular file of ours, in a
    folder only we can write; otherwise fail-closed. Opened once with
    O_NOFOLLOW and checked with fstat on that same descriptor, which is also the one read: no window
    between the check and the read in which the path could be swapped for a link."""
    parent = path.parent.lstat()
    if parent.st_uid != os.getuid() or parent.st_mode & (stat.S_IWGRP | stat.S_IWOTH):
        raise ValueError(f"{path.parent} ist für andere schreibbar oder gehört nicht dir ({what})")
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    except OSError:
        raise ValueError(f"{path} ist keine normale Datei oder nicht lesbar ({what})") from None
    with os.fdopen(fd, "r", encoding="utf-8", errors="replace") as handle:
        st = os.fstat(handle.fileno())
        if not stat.S_ISREG(st.st_mode):
            raise ValueError(f"{path} ist keine normale Datei ({what})")
        if st.st_uid != os.getuid():
            raise ValueError(f"{path} gehört nicht dir ({what})")
        if st.st_mode & 0o077:
            raise ValueError(f"{path} hat Rechte {oct(st.st_mode & 0o777)}, erwartet 0600 ({what}): chmod 600 {path}")
        return handle.read()


@dataclass(frozen=True)
class Config:
    servers: Dict[str, str] = field(default_factory=dict)
    # The person's own rules (rules.md), appended to every agent's working rules. The code ships none.
    rules: str = ""

    def server(self, name: str) -> Optional[str]:
        return self.servers.get(name)

    def has(self, name: str) -> bool:
        return name in self.servers


def _read(paths: Paths) -> Dict:
    if not paths.mesh_config.exists() and not paths.mesh_config.is_symlink():
        return {}
    text = read_private(paths.mesh_config, "moshi-cockpit-Config")
    try:
        data = json.loads(text)
    except ValueError:
        raise ValueError(f"{paths.mesh_config} ist nicht lesbar oder kein gültiges JSON; "
                         "moshi-cockpit install schreibt sie neu") from None
    if not isinstance(data, dict):
        raise ValueError(f"{paths.mesh_config} hat ein unerwartetes Format")
    return data


def load_config(paths: Paths) -> Config:
    """Fail-closed: a config that exists but is broken, or holds an invalid server URL, is an error."""
    data = _read(paths)
    raw = data.get("servers") or {}
    if not isinstance(raw, dict):
        raise ValueError(f"{paths.mesh_config}: 'servers' hat ein unerwartetes Format")
    servers = {}
    for name in SERVERS:
        if raw.get(name):
            servers[name] = validate_url(str(raw[name]))
    unknown = sorted(set(raw) - set(SERVERS))
    if unknown:
        raise ValueError(f"{paths.mesh_config}: unbekannte Server {unknown}")
    return Config(servers=servers, rules=load_rules(paths))


def save_servers(paths: Paths, backup: BackupStore, servers: Dict[str, Optional[str]]) -> bool:
    """Sets (URL) or removes (None/"") servers; the others stay as they are."""
    try:
        data = _read(paths)
    except ValueError:
        data = {}  # rewriting a broken config is the documented way out
    current = data.get("servers") if isinstance(data.get("servers"), dict) else {}
    for name, url in servers.items():
        if name not in SERVERS:
            raise ValueError(f"Unbekannter Server '{name}' (erlaubt: {', '.join(SERVERS)})")
        if url:
            current[name] = validate_url(url)
        else:
            current.pop(name, None)
    data["servers"] = current
    return backup.write_text(paths.mesh_config, json.dumps(data, indent=2) + "\n", mode=0o600)


def require(config: Config, name: str) -> str:
    """The URL of a server a command needs; a clear message if it is not configured."""
    url = config.server(name)
    if not url:
        raise ValueError(f"{name} ist nicht eingerichtet: moshi-cockpit install --{name} <https-URL>")
    return url


def moshi_base(url: str) -> str:
    """https://host/mcp → https://host (for /api/admin and /agents/connect)."""
    return url[: -len("/mcp")] if url.endswith("/mcp") else url.rstrip("/")


MAX_RULES = 4000


def _clean_rules(text: str) -> str:
    """Line breaks and tabs stay, other control characters go; bounded."""
    kept = "".join(ch for ch in text if ch in "\n\t" or not (ord(ch) < 32 or 127 <= ord(ch) < 160))
    return kept.strip()


def load_rules(paths: Paths) -> str:
    """The person's own rules for all agents (e.g. which data they must not touch). moshi-cockpit never
    writes this file on install, so what is in it stays; it is owner-only like the config (fail-closed)."""
    if not paths.rules_file.exists() and not paths.rules_file.is_symlink():
        return ""
    text = _clean_rules(read_private(paths.rules_file, "eigene Regeln"))
    if len(text) > MAX_RULES:
        raise ValueError(f"{paths.rules_file} ist länger als {MAX_RULES} Zeichen")
    return text


def save_rules(paths: Paths, backup: BackupStore, text: str) -> bool:
    text = _clean_rules(text)
    if len(text) > MAX_RULES:
        raise ValueError(f"Eigene Regeln: höchstens {MAX_RULES} Zeichen")
    return backup.write_text(paths.rules_file, (text + "\n") if text else "", mode=0o600)
