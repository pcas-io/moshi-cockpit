"""Host keys for the add flow of the Machines tab: is a machine's key known, which keys does it offer, and
trusting one of them.

The target is split like hc reads it (`alias`, `user@host`, `user@host:port`, `host:port`); `ssh -G` says which
host and port ssh really dials. `ssh -G` evaluates your ssh config like ssh itself (a `Match exec` in it runs),
so it is not a pure read, but it writes nothing and dials nothing. A host whose settings this module cannot follow
exactly (a proxy, a host key alias, other known_hosts files, hashed known_hosts) gets no scan and no trust: it is
added in a tab (NeedsTab). `known` asks `ssh-keygen -F` about ~/.ssh/known_hosts; `scan` runs `ssh-keyscan` and
computes each key's SHA256 fingerprint like `ssh-keygen -l`. `trust` scans again and appends the one scanned line
whose type and fingerprint are the ones the person saw, or nothing. No shell, a timeout on every process;
~/.ssh/config is never written, known_hosts is only appended to.
"""

from __future__ import annotations

import base64
import binascii
import fcntl
import hashlib
import os
import re
from pathlib import Path
from typing import Callable, Dict, List, NamedTuple, Optional, Sequence, Tuple

from cockpit.targets import check_target
from cockpit.text import clean

Runner = Callable[[Sequence[str], int], Tuple[int, str, str]]

LOCAL_TIMEOUT = 5  # ssh -G and ssh-keygen -F only read local files
SCAN_TIMEOUT = 12  # ssh-keyscan's own -T is 8 s
SCAN_WAIT = "8"
DEFAULT_PORT = 22
CHANGED = "The host key changed since you looked at it; nothing was trusted"

_USER_RE = re.compile(r"^[A-Za-z0-9._-]+$")
_HOST_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:%-]{0,253}$")
_TYPE_RE = re.compile(r"^[a-z0-9][a-z0-9@.-]{0,63}$")
_FP_RE = re.compile(r"^SHA256:[A-Za-z0-9+/]{43}$")
_ORDER = ("ed25519", "ecdsa", "rsa")  # offered in this order; the Machines tab shows the first


# What `ssh -G` prints for the defaults; anything else in these settings sends the host to a tab.
_USER_FILES = ("~/.ssh/known_hosts", "~/.ssh/known_hosts2")
_GLOBAL_FILES = "/etc/ssh/ssh_known_hosts /etc/ssh/ssh_known_hosts2"
_SETTINGS = ("user", "hostname", "port", "proxyjump", "proxycommand", "hostkeyalias", "userknownhostsfile",
             "globalknownhostsfile", "hashknownhosts")


class NeedsTab(ValueError):
    """The host uses ssh settings the dialog doesn't follow exactly; it is added in a tab instead."""


class Key(NamedTuple):
    type: str
    line: str  # exactly as ssh-keyscan printed it: `host type base64`
    sha256: str


def _port(text: str) -> int:
    if not text.isdigit() or not 0 < int(text) < 65536:
        raise ValueError(f"'{text}' is not a port")
    return int(text)


def split_target(target: str) -> Tuple[Optional[str], str, Optional[int]]:
    """(user or None, host, port or None) of a target hc accepts; ValueError for anything else."""
    check_target(target)
    user: Optional[str] = None
    rest = target
    if "@" in target:
        user, rest = target.split("@", 1)
        if not _USER_RE.fullmatch(user):
            raise ValueError(f"'{target}' has no user name ssh can use")
    port: Optional[int] = None
    if rest.count(":") == 1:
        rest, text = rest.split(":")
        port = _port(text)
    elif rest.count(":") > 1:
        raise ValueError(f"'{target}' is not a target ssh can use")
    if not _HOST_RE.fullmatch(rest):
        raise ValueError(f"'{target}' has no host ssh can use")
    return user, rest, port


def _first_line(text: str) -> str:
    for line in (text or "").splitlines():
        if line.strip() and not line.startswith("#"):
            return clean(line.strip(), "?", limit=300)
    return ""


def _unhandled(found: Dict[str, str], home: Path) -> List[str]:
    """The ssh settings of this host the dialog can't follow exactly, by their ssh_config names."""
    users = {" ".join(_USER_FILES), " ".join(f.replace("~", str(home), 1) for f in _USER_FILES)}
    which = [name for key, name in (("proxyjump", "ProxyJump"), ("proxycommand", "ProxyCommand"))
             if found.get(key, "none").lower() != "none"]
    which += ["HostKeyAlias"] if "hostkeyalias" in found else []
    which += ["UserKnownHostsFile"] if found.get("userknownhostsfile", " ".join(_USER_FILES)) not in users else []
    which += ["GlobalKnownHostsFile"] if found.get("globalknownhostsfile", _GLOBAL_FILES) != _GLOBAL_FILES else []
    which += ["HashKnownHosts"] if found.get("hashknownhosts", "no").lower() == "yes" else []
    return which


def resolve(target: str, runner: Runner, home: Path) -> Tuple[Optional[str], str, int]:
    """(user, host, port) ssh really uses for `target`, from `ssh -G` (an alias's HostName and Port); NeedsTab
    when its settings are ones the dialog can't follow exactly."""
    user, host, port = split_target(target)
    argv = ["ssh", "-G"] + (["-p", str(port)] if port else []) + [f"{user}@{host}" if user else host]
    code, out, err = runner(argv, LOCAL_TIMEOUT)
    if code != 0:
        raise ValueError(f"ssh cannot read its settings for {target}: {_first_line(err) or f'exit code {code}'}")
    found: Dict[str, str] = {}
    for line in out.splitlines():
        parts = line.split(None, 1)
        if len(parts) == 2 and parts[0] in _SETTINGS:
            found.setdefault(parts[0], parts[1].strip())
    real = found.get("hostname", "")
    if not _HOST_RE.fullmatch(real) or "port" not in found:
        raise ValueError(f"ssh gave no host ssh can use for {target}")
    which = _unhandled(found, Path(home))
    if which:
        raise NeedsTab(f"{target} uses ssh settings this dialog doesn't handle ({', '.join(which)}); add it in a tab.")
    return found.get("user") or user, real, _port(found["port"])


def _host_field(host: str, port: int) -> str:
    """How known_hosts and ssh-keyscan name the host: `[host]:port` unless the port is 22."""
    return host if port == DEFAULT_PORT else f"[{host}]:{port}"


def _known_hosts(home: Path) -> Path:
    return Path(home) / ".ssh" / "known_hosts"


def known(host: str, port: int, runner: Runner, home: Path) -> bool:
    code, _, _ = runner(["ssh-keygen", "-F", _host_field(host, port), "-f", str(_known_hosts(home))], LOCAL_TIMEOUT)
    return code == 0


def fingerprint(blob: str) -> str:
    """`SHA256:…` of a base64 public key, as `ssh-keygen -l` prints it."""
    try:
        raw = base64.b64decode(blob.encode("ascii"), validate=True)
    except (binascii.Error, UnicodeEncodeError, ValueError):
        raise ValueError("not a public key") from None
    if not raw:
        raise ValueError("not a public key")
    return "SHA256:" + base64.b64encode(hashlib.sha256(raw).digest()).decode("ascii").rstrip("=")


def _rank(key_type: str) -> int:
    for index, word in enumerate(_ORDER):
        if word in key_type:
            return index
    return len(_ORDER)


def _scan(host: str, port: int, runner: Runner) -> Tuple[List[Key], str]:
    argv = ["ssh-keyscan", "-T", SCAN_WAIT] + (["-p", str(port)] if port != DEFAULT_PORT else []) + [host]
    code, out, err = runner(argv, SCAN_TIMEOUT)
    field, keys = _host_field(host, port), []
    for line in out.splitlines():
        parts = line.split()
        if line.startswith("#") or len(parts) != 3 or parts[0] != field or not _TYPE_RE.fullmatch(parts[1]):
            continue
        try:
            key = Key(parts[1], " ".join(parts), fingerprint(parts[2]))
        except ValueError:
            continue
        if key not in keys:
            keys.append(key)
    keys.sort(key=lambda k: _rank(k.type))
    return keys, _first_line(err) or (f"exit code {code}" if code else "no host key")


def scan(host: str, port: int, runner: Runner) -> List[Key]:
    """The keys the machine offers for exactly this host and port, ed25519 first, then ecdsa, then rsa."""
    return _scan(host, port, runner)[0]


def hostkey(target: str, runner: Runner, home: Path) -> Dict:
    """What the Machines tab shows before adding: known, or the keys to choose from (a known key is not scanned),
    or `needs: "tab"` for a host the dialog can't handle exactly (no scan then)."""
    try:
        _, host, port = resolve(target, runner, home)
    except NeedsTab as exc:
        return {"target": target, "needs": "tab", "message": str(exc)}
    answer: Dict = {"target": target, "host": host, "port": port, "known": known(host, port, runner, home), "keys": []}
    if answer["known"]:
        return answer
    keys, why = _scan(host, port, runner)
    if not keys:
        raise ValueError(f"{target} did not answer: {why}")
    answer["keys"] = [{"type": k.type, "sha256": k.sha256} for k in keys]
    return answer


def _append(path: Path, line: str) -> None:
    """One line at the end of known_hosts, under a lock; nothing else in the file changes."""
    if not path.parent.exists():
        path.parent.mkdir(mode=0o700, parents=True)
        os.chmod(path.parent, 0o700)
    fd = os.open(str(path), os.O_RDWR | os.O_APPEND | os.O_CREAT, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        size = os.fstat(fd).st_size
        lead = ""
        if size:
            os.lseek(fd, size - 1, os.SEEK_SET)
            lead = "" if os.read(fd, 1) == b"\n" else "\n"
        os.write(fd, (lead + line + "\n").encode("ascii"))
    finally:
        os.close(fd)  # releases the lock


def trust(host: str, port: int, key_type: str, sha256: str, runner: Runner, home: Path,
          target: Optional[str] = None) -> None:
    """Scans again and appends the line of the key of `key_type` with fingerprint `sha256`; ValueError (CHANGED)
    when the machine no longer offers exactly that key, "<target> did not answer: …" when it offers none."""
    if not _TYPE_RE.fullmatch(key_type or "") or not _FP_RE.fullmatch(sha256 or "") or not _HOST_RE.fullmatch(host):
        raise ValueError("Not a host key to trust")
    keys, why = _scan(host, port, runner)
    if not keys:
        raise ValueError(f"{target or host} did not answer: {why}")
    match = [k for k in keys if k.type == key_type and k.sha256 == sha256]
    if not match:
        raise ValueError(CHANGED)
    _append(_known_hosts(home), match[0].line)
