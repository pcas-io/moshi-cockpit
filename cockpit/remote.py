"""moshi-cockpit on a remote Linux machine, seen from the Mac: its state, the tab commands for it, and forwarded
`agent add | load | catalog` calls.

Everything goes through `hc exec --on <machine>`, which passes argv intact. The probe is `PROBE_SCRIPT`, static;
the remote login shell may print a MOTD around it, so only marker lines count and the answer must end with the
`end` marker. States are cached in `state/remote-cockpit.json` (0600) for a minute; a failed probe never
overwrites the cache. An add or load that answered 0 probes once more, so the new agent shows at once. hc mixes
its own exit codes with the remote program's: only 0, 1 and 3 are the remote's answer, 127 means moshi-cockpit
is missing there, anything else that the machine did not answer.
"""

from __future__ import annotations

import fcntl
import json
import os
import re
import tempfile
import time
from datetime import datetime, timezone
from typing import Callable, Dict, List, Optional, Tuple

from cockpit import __version__
from cockpit.agents_remote import APPROVAL
from cockpit.config import load_config
from cockpit.harnesses import HARNESSES
from cockpit.machines import Runner, first_line, hc_path
from cockpit.paths import Paths
from cockpit.registry import MAX_ROLE, NAME_RE, bound_role, validate_name
from cockpit.targets import check_target
from cockpit.text import clean

MARK = "moshi-cockpit-remote"
PROBE_TIMEOUT = 45  # the runner's guard; hc's own --timeout for the remote program is shorter
REMOTE_TIMEOUT = "30"
AGENT_TIMEOUT = 120  # an add registers in moshi from the remote: slower than the probe
REMOTE_AGENT_TIMEOUT = "90"
CACHE_SECONDS = 60
CACHE_FILE = "remote-cockpit.json"
LOCK_FILE = "remote-cockpit.lock"
INSTALL_URL = f"https://github.com/pcas-io/moshi-cockpit/releases/download/v{__version__}/install.sh"

PROBE_SCRIPT = r"""printf 'moshi-cockpit-remote\tos\t%s\n' "$(uname -s)"
c="$HOME/.local/bin/moshi-cockpit"
if [ -x "$c" ]; then
  printf 'moshi-cockpit-remote\tlist\t%s\n' "$("$c" agent list --json 2>/dev/null | tr -d '\n')"
else
  printf 'moshi-cockpit-remote\tlist\tmissing\n'
fi
printf 'moshi-cockpit-remote\tend\n'
exit 0"""
CLI_SCRIPT = 'c="$HOME/.local/bin/moshi-cockpit"; [ -x "$c" ] || exit 127; exec "$c" "$@"'
# $1 install.sh URL, $2 moshi, $3 ocellio: an empty server is not passed, so the remote keeps the one it has. The
# temp file goes on every exit, also when the tab is closed (HUP) or interrupted.
INSTALL_SCRIPT = r"""u=$1; m=$2; o=$3; set --
if [ -n "$m" ]; then set -- "$@" --moshi "$m"; fi
if [ -n "$o" ]; then set -- "$@" --ocellio "$o"; fi
t=$(mktemp) && trap 'rm -f "$t"' EXIT && trap 'exit 129' HUP && trap 'exit 130' INT && trap 'exit 143' TERM \
  && curl --proto =https --tlsv1.2 -fsSL -o "$t" "$u" && sh "$t" "$@"
code=$?
printf '\nFinished (exit %s). Press Enter to close.\n' "$code"
read -r _"""

STATES = ("unsupported", "missing", "unconnected", "connected", "error")
ACTIONS = ("add", "load", "catalog")
OWN_HARNESSES = tuple(name for name, h in HARNESSES.items() if not h.no_own_agents)  # those that take an agent
NO_HC = "Herdr's hc is not available on this Mac"
ANSWERS = (0, 1, 3)  # exit codes that come from moshi-cockpit there, not from hc or ssh
VERSION_RE = re.compile(r"^\d+\.\d+\.\d+$")
UNEXPECTED = "moshi-cockpit there answered something unexpected; run moshi-cockpit doctor there"
UNREADABLE_VERSION = ("moshi-cockpit there reports no version this Mac can read, so it is not offered an update;"
                      " run moshi-cockpit doctor there")
MAX_TEXT = 300


def probe_argv(paths: Paths, machine: str) -> List[str]:
    return [hc_path(paths), "exec", "--on", machine, "--timeout", REMOTE_TIMEOUT, "/bin/sh", "-lc", PROBE_SCRIPT]


def connect_argv(machine: str) -> List[str]:
    return ["/bin/sh", "-lc", 'exec "$HOME/.local/bin/moshi-cockpit" connect --label "$1"', "sh", machine]


def run_argv(name: str) -> List[str]:
    return ["/bin/sh", "-lc", 'exec "$HOME/.local/bin/moshi-cockpit" run "$1"', "sh", name]


def install_argv(paths: Paths, update: bool = False) -> List[str]:
    """A held tab on the machine: this Mac's version of install.sh, verified downloads, this Mac's servers for a
    new install; an update passes none, so the machine keeps its own (install.sh shows them, Enter keeps them)."""
    servers = {} if update else load_config(paths).servers
    return ["/bin/sh", "-lc", INSTALL_SCRIPT, "sh", INSTALL_URL, servers.get("moshi", ""), servers.get("ocellio", "")]


def _agents(rows: object) -> List[Dict]:
    """Agents with a valid name and a known harness; roles without control characters."""
    found = []
    for row in rows if isinstance(rows, list) else []:
        if not isinstance(row, dict) or row.get("harness") not in HARNESSES:
            continue
        try:
            name = validate_name(row.get("name") if isinstance(row.get("name"), str) else "")
        except ValueError:
            continue
        role = row.get("role")
        found.append({"name": name, "harness": row["harness"], "role": clean(role if isinstance(role, str) else "",
                                                                             "", limit=200)})
    return found


def _harnesses(rows: object) -> List[Dict]:
    found = []
    for row in rows if isinstance(rows, list) else []:
        if isinstance(row, dict) and row.get("name") in HARNESSES:
            reason = row.get("reason")
            found.append({"name": row["name"], "installed": row.get("installed") is True,
                          "available": row.get("available") is True,
                          "reason": clean(reason if isinstance(reason, str) else "", "", limit=MAX_TEXT)})
    return found


def _version(text: object) -> Optional[Tuple[int, ...]]:
    """Parses "1.2.3" as (1, 2, 3); None for anything else."""
    if not isinstance(text, str) or not VERSION_RE.fullmatch(text):
        return None
    return tuple(int(part) for part in text.split("."))


def parse_probe(stdout: str) -> Dict:
    """The probe's marker lines (the first of each kind) as a state; any other line is noise. ValueError
    ("incomplete answer") without the `end` marker."""
    marks: Dict[str, str] = {}
    for line in (stdout or "").splitlines():
        parts = line.rstrip("\r").split("\t", 2)
        if parts[0] == MARK and len(parts) > 1:
            marks.setdefault(parts[1], parts[2] if len(parts) > 2 else "")
    if "end" not in marks or "os" not in marks or "list" not in marks:
        raise ValueError("incomplete answer")
    result: Dict = {"os": clean(marks["os"], "", limit=40), "state": "error", "version": None, "agents": [],
                    "harnesses": [], "ocellio": False, "message": ""}
    if result["os"] != "Linux":
        return dict(result, state="unsupported")
    if marks["list"] == "missing":
        return dict(result, state="missing")
    try:
        data = json.loads(marks["list"])
    except ValueError:
        data = None
    if not isinstance(data, dict) or not isinstance(data.get("agents"), list):
        return dict(result, message=UNEXPECTED)
    version, servers = data.get("version"), data.get("servers")
    if _version(version) is None:
        return dict(result, message=UNREADABLE_VERSION)
    return dict(result, state="connected" if data.get("connected") is True else "unconnected", version=version,
                agents=_agents(data["agents"]), harnesses=_harnesses(data.get("harnesses")),
                ocellio=isinstance(servers, dict) and servers.get("ocellio") is True)


def _cache_path(paths: Paths):
    return paths.state / CACHE_FILE


def _valid(entry: object) -> bool:
    return (isinstance(entry, dict) and isinstance(entry.get("at"), (int, float)) and entry.get("state") in STATES
            and isinstance(entry.get("agents"), list))


def _load_cache(paths: Paths) -> Dict:
    try:
        data = json.loads(_cache_path(paths).read_text())
    except (OSError, ValueError):
        return {}
    return {k: v for k, v in data.items() if _valid(v)} if isinstance(data, dict) else {}


def _store(paths: Paths, key: str, entry: Optional[Dict]) -> None:
    """Merges one machine's entry into the cache, or drops it (`entry` None), under a lock: probes of several
    machines run in parallel. Each writer has a temp file of its own (mkstemp: 0600)."""
    paths.state.mkdir(parents=True, exist_ok=True)
    lock = os.open(str(paths.state / LOCK_FILE), os.O_RDWR | os.O_CREAT, 0o600)
    try:
        fcntl.flock(lock, fcntl.LOCK_EX)
        cache = _load_cache(paths)
        if entry is None:
            cache.pop(key, None)
        else:
            cache[key] = entry
        fd, tmp = tempfile.mkstemp(dir=str(paths.state), prefix=".remote-cockpit.", suffix=".tmp")
        try:
            with os.fdopen(fd, "w") as handle:
                json.dump(cache, handle, sort_keys=True)
            os.replace(tmp, _cache_path(paths))
        except BaseException:
            os.unlink(tmp)
            raise
    finally:
        os.close(lock)  # releases the lock


def forget(paths: Paths, machine: str) -> None:
    """Drops the machine's cached state; a cache that cannot be written only means a stale entry."""
    try:
        _store(paths, machine, None)
    except OSError:
        pass


def _probe(paths: Paths, machine: str, runner: Runner) -> Dict:
    code, out, err = runner(probe_argv(paths, machine), PROBE_TIMEOUT)
    if code == 127 and not os.path.exists(hc_path(paths)):
        raise ValueError(NO_HC)
    if code != 0:
        raise ValueError(f"{machine} did not answer: {first_line(err) or f'exit code {code}'}")
    try:
        return parse_probe(out)
    except ValueError as exc:
        raise ValueError(f"{machine} did not answer: {exc}") from None


def _reprobe(paths: Paths, machine: str, runner: Runner) -> None:
    """After an add or load there: the machine's state once more (best effort), so its new agent shows at once;
    the cached entry goes only when that probe fails."""
    try:
        _store(paths, machine, dict(_probe(paths, machine, runner), at=time.time()))
    except (ValueError, OSError):
        forget(paths, machine)


def _state_text(state: str, count: int, message: str, update: bool) -> str:
    text = {"unsupported": "", "missing": "moshi-cockpit not installed",
            "unconnected": "moshi-cockpit not connected to moshi",
            "connected": f"moshi-cockpit connected ({count} agent{'s' if count != 1 else ''})"}.get(state, message)
    return text + (f" · update to {__version__}" if update else "")


def cockpit_state(paths: Paths, machine: str, runner: Runner, refresh: bool = False,
                  now: Callable[[], float] = time.time) -> Dict:
    """moshi-cockpit on `machine`, from the cache when it is younger than a minute and `refresh` is not set, with
    the tab commands that apply: install or update on Linux, connect once it is installed there."""
    check_target(machine)
    moment = now()
    entry = _load_cache(paths).get(machine)
    cached = not refresh and entry is not None and 0 <= moment - entry["at"] < CACHE_SECONDS
    if not cached:
        entry = dict(_probe(paths, machine, runner), at=moment)
        _store(paths, machine, entry)
    state, linux = entry["state"], entry.get("os") == "Linux"
    version = entry.get("version")
    found = _version(version) if state in ("unconnected", "connected") else None
    update = linux and found is not None and found < (_version(__version__) or ())  # never a downgrade
    agents = [dict(a, run_argv=run_argv(a["name"])) for a in _agents(entry["agents"])]
    return {"machine": machine,
            "checked_at": datetime.fromtimestamp(entry["at"], timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "cached": cached, "os": entry.get("os") or "", "state": state,
            "state_text": _state_text(state, len(agents), clean(entry.get("message"), "", limit=MAX_TEXT), update),
            "version": version, "update": update, "agents": agents, "harnesses": _harnesses(entry.get("harnesses")),
            "ocellio": entry.get("ocellio") is True,
            "install_argv": install_argv(paths, update) if linux and (state == "missing" or update) else None,
            "connect_argv": connect_argv(machine) if linux and state in ("unconnected", "connected") else None}


def cached_agents(paths: Paths) -> List[Dict]:
    """The agents of every machine whose cached moshi-cockpit is connected, however old the entry."""
    found = []
    for machine, entry in _load_cache(paths).items():
        try:
            check_target(machine)
        except ValueError:
            continue
        if entry["state"] == "connected":
            found += [dict(a, machine=machine, run_argv=run_argv(a["name"])) for a in _agents(entry["agents"])]
    return sorted(found, key=lambda a: (a["machine"], a["name"]))


def check_role(role: object) -> str:
    """The role rule of the dialog's roleText (rules.js), refused here instead of cleaned: one line, no control or
    format characters (Unicode category C: escapes, zero-width and direction marks), at most MAX_ROLE UTF-16 units
    as moshi counts them. ValueError names the reason."""
    if not isinstance(role, str):
        raise ValueError("The role must be text")
    if any(ch in "\n\r\u2028\u2029" for ch in role):
        raise ValueError("The role must be one line")
    if clean(role, "") != role:
        raise ValueError("The role must not contain control or format characters")
    if bound_role(role) != role:
        raise ValueError(f"The role must be at most {MAX_ROLE} characters")
    return role


def _message(err: str) -> str:
    """The remote's one line: its `Error: …` (without the prefix) or approval line, else the first line at all
    (a login shell may print a locale warning before it)."""
    lines = [line.strip() for line in (err or "").splitlines() if line.strip()]
    for line in lines:
        if line.startswith("Error:"):
            return clean(line[len("Error:"):].strip(), "?", limit=MAX_TEXT)
        if line.startswith(("Approval", APPROVAL.split(":", 1)[0])):  # the remote's own approval sentence too
            return clean(line, "?", limit=MAX_TEXT)
    return first_line(err)


def _catalog(out: str) -> Optional[List[Dict]]:
    """The rows of the last JSON line of `agent catalog --json`, validated; None when there is none."""
    data = None
    for line in reversed((out or "").splitlines()):
        if line.startswith("{"):
            try:
                data = json.loads(line)
            except ValueError:
                continue
            break
    if not isinstance(data, dict) or not isinstance(data.get("agents"), list):
        return None
    rows = []
    for row in data["agents"]:
        if not isinstance(row, dict):
            continue
        try:
            name = validate_name(row.get("name") if isinstance(row.get("name"), str) else "")
        except ValueError:
            continue

        def text(key: str, limit: int) -> str:
            value = row.get(key)
            return clean(value if isinstance(value, str) else "", "", limit=limit)

        rows.append({"name": name, "harness": text("harness", 40), "role": text("role", MAX_TEXT),
                     "active_installation": text("active_installation", 64) or None,
                     "loadable": row.get("loadable") is True, "harness_installed": row.get("harness_installed") is True,
                     "reason": text("reason", MAX_TEXT)})
    return rows


def agent_call(paths: Paths, machine: str, action: str, runner: Runner, name: Optional[str] = None,
               harness: Optional[str] = None, role: str = "", skill: str = "") -> Tuple[int, Dict]:
    """`moshi-cockpit agent add | load | catalog` on `machine`, validated here before anything is sent. Returns
    the exit code (0/1/3) and the answer."""
    check_target(machine)
    if action not in ACTIONS:
        raise ValueError(f"Unknown action '{clean(action, '?', limit=20)}' (known: {', '.join(ACTIONS)})")
    tail = ["catalog", "--json"]
    if action in ("add", "load"):
        validate_name(name or "")
        tail = ["load", name]
    check_role(role)
    if not isinstance(skill, str) or (skill and not NAME_RE.fullmatch(skill)):  # the dialog's checkSkill
        raise ValueError("Invalid skill name (letters, digits, _ and -)")
    if action == "add":
        if harness not in OWN_HARNESSES:
            raise ValueError(f"Unknown harness '{clean(harness, '', limit=40)}'. Allowed: {', '.join(OWN_HARNESSES)}")
        tail = ["add", name, "--harness", harness, f"--role={role}", f"--skill={skill}"]  # "=": a value may start with -
    argv = [hc_path(paths), "exec", "--on", machine, "--timeout", REMOTE_AGENT_TIMEOUT, "/bin/sh", "-lc", CLI_SCRIPT,
            "sh", "agent"] + tail
    code, out, err = runner(argv, AGENT_TIMEOUT)
    notes: List[str] = []
    if code in ANSWERS:
        notes = [clean(line.rstrip("\r")[2:], "", limit=500) for line in (out or "").splitlines() if line.startswith("• ")]
        message = _message(err)
        if action != "catalog":
            if code == 0:
                _reprobe(paths, machine, runner)
            else:
                forget(paths, machine)
    elif code == 127 and not os.path.exists(hc_path(paths)):
        code, message = 1, NO_HC
    elif code == 127:
        code, message = 1, f"moshi-cockpit is not installed on {machine}: install it under Machines"
    else:
        code, message = 1, f"{machine} did not answer: {first_line(err) or f'exit code {code}'}"
    answer: Dict = {"ok": code == 0, "code": code, "notes": notes, "message": message,
                    "run_argv": run_argv(name) if code == 0 and action != "catalog" else None}
    if action == "catalog":
        rows = _catalog(out) if code == 0 else []
        if rows is None:
            code, rows = 1, []
            answer.update(ok=False, code=1, message=f"{machine} did not answer: incomplete answer")
        answer["agents"] = rows
    return code, answer
