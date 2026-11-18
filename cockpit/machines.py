"""`moshi-cockpit machine list | inventory | add | remove`: the machines Herdr reaches over ssh and the agent CLIs on each.

Every machine name or ssh alias that reaches hc's argv passes `check_target` first: nothing that hc could read
as an option, no spaces, no shell characters. The probe is `inventory.probe_script()`, static; its states are
cached in `state/inventory.json` (0600, states only) for a minute. A failed probe never overwrites the cache.
"""

from __future__ import annotations

import fcntl
import json
import os
import signal
import subprocess
import tempfile
import time
from datetime import datetime, timezone
from typing import Callable, Dict, List, Optional, Sequence, Set, Tuple

from cockpit import hosts
from cockpit.backup import BackupStore
from cockpit.harness_install import INSTALL, shown_command
from cockpit.inventory import (BINARIES, INSTALLED, KNOWN, LABELS, LOGIN, MARK, MISSING, PATH_EXTRA, STATE_TEXT,
                               held_argv, parse_probe, probe_script)
from cockpit.paths import Paths
from cockpit.ssh_config import read_hosts
from cockpit.targets import NAME_RE, check_target
from cockpit.text import clean

Runner = Callable[[Sequence[str], int], Tuple[int, str, str]]

LIST_TIMEOUT = 15
PROBE_TIMEOUT = 45  # the runner's guard; hc's own --timeout for the remote program is shorter
REMOTE_TIMEOUT = "30"
CACHE_SECONDS = 60
REMOVE_TIMEOUT = 30  # `hc machine rm`: Herdr forgets the machine; its terminals keep running there
ADD_TIMEOUT = 120  # `machine add --batch`: hc dials, copies itself there and checks that it answers
# add_batch's worst case: ls, add, ls, probe (the mod's exec waits longer: ADD_EXEC in machines.js, tested)
ADD_WORST = LIST_TIMEOUT + ADD_TIMEOUT + LIST_TIMEOUT + PROBE_TIMEOUT
CACHE_FILE = "inventory.json"
LOCK_FILE = "inventory.lock"
THIS_MAC = "This Mac"
# The probe prints its curl line last: without it the answer was cut off.
END_LINES = tuple(f"{MARK}\tneed\tcurl\t{flag}" for flag in ("0", "1"))


def run_capture(argv: Sequence[str], timeout: int) -> Tuple[int, str, str]:
    """The real runner: no terminal, output captured. A program that is missing gives 127, one too slow 124."""
    try:
        done = subprocess.run(list(argv), capture_output=True, text=True, errors="replace", timeout=timeout,
                              stdin=subprocess.DEVNULL)
    except subprocess.TimeoutExpired:
        return 124, "", f"no answer within {timeout} s"
    except OSError as exc:
        return 127, "", exc.strerror or str(exc)
    return done.returncode, done.stdout, done.stderr


def run_batch(argv: Sequence[str], timeout: int) -> Tuple[int, str, str]:
    """Like run_capture, in a session of its own: without a terminal to ask on, nothing ssh starts can prompt. On
    a timeout the whole process group goes (hc and the ssh and scp it started), not only hc."""
    try:
        proc = subprocess.Popen(list(argv), stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                text=True, errors="replace", start_new_session=True)
    except OSError as exc:
        return 127, "", exc.strerror or str(exc)
    try:
        out, err = proc.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except OSError:
            pass  # already gone
        try:
            proc.communicate(timeout=5)
        except subprocess.TimeoutExpired:
            pass  # a child that left the group still holds the pipes; hc itself is dead
        return 124, "", f"no answer within {timeout} s"
    return proc.returncode, out, err


def _first_line(text: str) -> str:
    for line in (text or "").splitlines():
        if line.strip():
            return clean(line.strip(), "?", limit=300)
    return ""


def _hc(paths: Paths) -> str:
    return str(paths.herdr_core_bin)


def _herdr_machines(paths: Paths, runner: Runner) -> List[Dict]:
    """`hc machine ls --json` without temporary machines; ValueError with one line when hc cannot answer."""
    code, out, err = runner([_hc(paths), "machine", "ls", "--json"], LIST_TIMEOUT)
    if code != 0:
        raise ValueError(f"Herdr's machine list is not available: {_first_line(err) or f'exit code {code}'}")
    try:
        found = json.loads(out).get("machines")
    except (ValueError, AttributeError):
        found = None
    if not isinstance(found, list):
        raise ValueError("Herdr's machine list is not available: hc answered something unexpected")
    return [m for m in found if isinstance(m, dict) and not m.get("temporary")]


def _known_as(entry: Dict) -> Set[str]:
    """Every ssh alias that means this machine: its name, host, target and the target's host after '@'."""
    target = str(entry.get("target") or "")
    names = {str(entry.get("name") or ""), str(entry.get("host") or ""), target, target.rsplit("@", 1)[-1]}
    return names - {""}


def _logout(entry: Dict) -> Dict:
    """hc's answer whether programs there outlive the last login (None: unknown), and the user its fix
    (`loginctl enable-linger`) names, only when that user passes the name rule (None otherwise)."""
    keeps, user = entry.get("keeps_running_after_logout"), entry.get("logout_linger_user")
    return {"keeps_running_after_logout": keeps if isinstance(keeps, bool) else None,
            "logout_linger_user": user if isinstance(user, str) and NAME_RE.fullmatch(user) else None}


def list_machines(paths: Paths, runner: Runner) -> Dict:
    result: Dict = {"machines": []}
    known: Set[str] = set()
    try:
        for entry in _herdr_machines(paths, runner):
            row = {key: str(entry.get(key) or "") for key in ("name", "state", "target")}
            result["machines"].append(dict(row, **_logout(entry)))
            known |= _known_as(entry)
    except ValueError as exc:
        result["problem"] = str(exc)
    result["ssh_hosts"] = [alias for alias in read_hosts(paths.home) if alias not in known]
    try:
        saved = hosts.load_hosts(paths)
    except ValueError as exc:
        saved = []
        result["problem"] = "; ".join(filter(None, [result.get("problem"), str(exc)]))
    result["saved_hosts"] = [t for t in saved if t not in known and t not in result["ssh_hosts"]]
    return result


def _cache_path(paths: Paths):
    return paths.state / CACHE_FILE


def _load_cache(paths: Paths) -> Dict:
    try:
        data = json.loads(_cache_path(paths).read_text())
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _valid(entry: object) -> bool:
    if not isinstance(entry, dict) or not isinstance(entry.get("at"), (int, float)):
        return False
    states = entry.get("states")
    return (isinstance(entry.get("has_curl"), bool) and isinstance(states, dict)
            and all(states.get(name) in STATE_TEXT for name in KNOWN))


def _store(paths: Paths, key: str, entry: Optional[Dict]) -> None:
    """Merges one machine's entry into the cache, or drops it (`entry` None). Inventories of several machines run
    in parallel: the lock keeps load, merge and save of one from losing another's entry; each writer has a temp
    file of its own."""
    paths.state.mkdir(parents=True, exist_ok=True)
    lock = os.open(str(paths.state / LOCK_FILE), os.O_RDWR | os.O_CREAT, 0o600)
    try:
        fcntl.flock(lock, fcntl.LOCK_EX)
        cache = {k: v for k, v in _load_cache(paths).items() if _valid(v)}
        if entry is None:
            cache.pop(key, None)
        else:
            cache[key] = entry
        fd, tmp = tempfile.mkstemp(dir=str(paths.state), prefix=".inventory.", suffix=".tmp")  # 0600
        try:
            with os.fdopen(fd, "w") as handle:
                json.dump(cache, handle, sort_keys=True)
            os.replace(tmp, _cache_path(paths))
        except BaseException:
            os.unlink(tmp)
            raise
    finally:
        os.close(lock)  # releases the lock


def _probe(paths: Paths, machine: Optional[str], runner: Runner) -> Dict:
    if machine is None:
        argv = ["/bin/sh", "-c", probe_script()]
    else:
        argv = [_hc(paths), "exec", "--on", machine, "--timeout", REMOTE_TIMEOUT, "/bin/sh", "-lc", probe_script()]
    code, out, err = runner(argv, PROBE_TIMEOUT)
    if code != 0:
        raise ValueError(f"{machine or THIS_MAC} did not answer: {_first_line(err) or f'exit code {code}'}")
    if not any(line.rstrip("\r") in END_LINES for line in out.splitlines()):
        raise ValueError(f"{machine or THIS_MAC} did not answer: incomplete answer")
    probe = parse_probe(out)
    return {"has_curl": probe.has_curl, "states": dict(probe.states)}


def _row(paths: Paths, name: str, state: str, machine: Optional[str], has_curl: bool) -> Dict:
    spec, missing = INSTALL[name], state == MISSING
    install_argv: Optional[List[str]] = None
    if missing and machine is None:
        install_argv = [str(paths.local_bin / "moshi-cockpit"), "harness", "install", name]
    elif missing and has_curl:
        install_argv = held_argv(shown_command(spec))
    return {"name": name, "label": LABELS[name], "state": state, "state_text": STATE_TEXT[state],
            "install_argv": install_argv,
            "install_command": shown_command(spec) if missing else None,
            "source_url": spec.source_url if missing else None,
            "login_argv": held_argv(" ".join(LOGIN[name])) if state == INSTALLED else None,
            "start_argv": ["/bin/sh", "-lc", f'PATH="$PATH:{PATH_EXTRA}"; exec {BINARIES[name]}']}


def inventory(paths: Paths, machine: Optional[str], runner: Runner, refresh: bool = False,
              now: Callable[[], float] = time.time) -> Dict:
    """The six harnesses on this Mac (`machine is None`) or on a Herdr machine, from the cache when it is
    younger than a minute and `refresh` is not set."""
    if machine is not None:
        check_target(machine)
    key, moment = machine or "", now()
    entry = _load_cache(paths).get(key)
    cached = not refresh and _valid(entry) and 0 <= moment - entry["at"] < CACHE_SECONDS
    if not cached:
        entry = dict(_probe(paths, machine, runner), at=moment)
        _store(paths, key, entry)
    checked = datetime.fromtimestamp(entry["at"], timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    return {"machine": machine, "checked_at": checked, "cached": cached, "has_curl": entry["has_curl"],
            "harnesses": [_row(paths, name, entry["states"][name], machine, entry["has_curl"]) for name in KNOWN]}


def remove_machine(paths: Paths, name: str, runner: Runner) -> Dict:
    """`hc machine rm`: Herdr forgets the machine, its terminals keep running there. Its cached inventory goes too;
    the saved host (hosts.json) stays, so it can be added again. ValueError with one line when hc refuses."""
    check_target(name)
    code, out, err = runner([_hc(paths), "machine", "rm", "--", name], REMOVE_TIMEOUT)
    if code != 0:
        message = clean(str(_hc_error(out, err).get("message") or ""), "", limit=300)
        raise ValueError(f"{name} was not removed: {message or _first_line(err) or _first_line(out) or f'exit code {code}'}")
    try:
        _store(paths, name, None)
    except OSError:
        pass  # Herdr has forgotten it; a stale cache entry only lives a minute
    return {"ok": True, "machine": name}


def _added_name(alias: str, before: Optional[Set[str]], after: List[Dict]) -> Optional[str]:
    """The machine hc just added for `alias`, only among the entries that are new (all of them when the list
    before could not be read): the one called by the alias, else the only new one, else None."""
    new = [e for e in after if str(e.get("name") or "") and (before is None or str(e.get("name")) not in before)]
    for matches in (lambda e: alias in (e.get("name"), e.get("host")), lambda e: alias in _known_as(e)):
        for entry in new:
            if matches(entry):
                return str(entry["name"])
    return str(new[0]["name"]) if len(new) == 1 else None


def add_machine(paths: Paths, alias: str, call: Callable[[Sequence[str]], int], runner: Runner,
                say: Callable[[str], None] = print) -> int:
    """`hc machine add` attached to the terminal (host key, password and 2FA prompts appear there), then the
    new machine's inventory. Returns hc's exit code."""
    check_target(alias)
    try:
        before: Optional[Set[str]] = {str(m.get("name")) for m in _herdr_machines(paths, runner)}
    except ValueError:
        before = None
    code = call([_hc(paths), "machine", "add", "--", alias])
    if code != 0:
        return code
    try:
        name = _added_name(alias, before, _herdr_machines(paths, runner))
        if name is None:
            say(f"{alias} was added; Herdr does not list it yet. Open the Machines tab to check it.")
            return code
        rows = inventory(paths, name, runner, refresh=True)["harnesses"]
    except (ValueError, OSError) as exc:
        say(f"{alias} was added; {clean(str(exc), '?', limit=500)}")
        return code
    for row in rows:
        say(f"{row['label']:<14}{row['state_text']}")
    return code


# hc's error kinds that mean a login it can't do without a person; ssh's words for a password and for keys only.
_LOGIN_KINDS = ("needs_login", "login_needed", "auth_failed")
_PASSWORD_WORDS = ("password", "keyboard-interactive")
_HOST_KEY_WORDS = ("host key verification failed", "host identification has changed", "host key is known for")


def _hc_error(*texts: str) -> Dict:
    """`{"kind", "message"}` of hc's JSON error, from whichever output carries it; {} when none does."""
    for text in texts:
        for chunk in [text] + (text or "").splitlines():
            try:
                found = json.loads(chunk)
            except ValueError:
                continue
            error = found.get("error") if isinstance(found, dict) else None
            if isinstance(error, dict):
                return error
    return {}


def _new_name(out: str) -> Optional[str]:
    try:
        found = json.loads(out)
    except ValueError:
        return None
    if isinstance(found, dict) and isinstance(found.get("machine"), dict):
        found = found["machine"]
    name = found.get("name") if isinstance(found, dict) else None
    return name if isinstance(name, str) and name else None


def add_batch(paths: Paths, store: BackupStore, target: str, runner: Runner, batch: Optional[Runner] = None) -> Dict:
    """`hc machine add --install` without a terminal (the Machines tab's add): saves `target` to the own list
    unless it is a Host of ~/.ssh/config, then answers {ok, machine, inventory} or {ok: false, needs?, message}.
    hc cannot ask anything here: a password or an unknown host key comes back as `needs`."""
    check_target(target)
    saved = True
    if target not in read_hosts(paths.home):
        try:
            hosts.add_host(paths, store, target)
        except (ValueError, OSError):  # a broken or unsafe hosts.json never blocks the add
            saved = False
    note = {} if saved else {"saved": False}
    try:
        before: Optional[Set[str]] = {str(m.get("name")) for m in _herdr_machines(paths, runner)}
    except ValueError:
        before = None
    code, out, err = (batch or runner)([_hc(paths), "machine", "add", "--install", "--json", "--", target], ADD_TIMEOUT)
    if code != 0:
        error = _hc_error(out, err)
        message = clean(str(error.get("message") or ""), "", limit=300) or _first_line(err) or _first_line(out) \
            or f"hc machine add ended with exit code {code}"
        said = f"{message}\n{err}\n{out}".lower()
        if any(word in said for word in _PASSWORD_WORDS):
            return dict(note, ok=False, needs="password", message=message)
        if "permission denied" in said:
            return dict(note, ok=False, needs="key", message=f"{target} accepts only ssh keys and none of yours worked.")
        if error.get("kind") == "host_key" or any(word in said for word in _HOST_KEY_WORDS):
            return dict(note, ok=False, needs="host_key", message=message)
        if error.get("kind") in _LOGIN_KINDS:
            return dict(note, ok=False, needs="tab", message=message)
        return dict(note, ok=False, message=message)
    answer: Dict = dict(note, ok=True, machine=None, inventory=None)
    try:
        name = _new_name(out) or _added_name(target, before, _herdr_machines(paths, runner))
        if name is None:
            return dict(answer, message=f"{target} was added; Herdr does not list it yet.")
        answer["machine"] = name
        answer["inventory"] = inventory(paths, name, runner, refresh=True)
    except (ValueError, OSError) as exc:
        answer["message"] = f"{target} was added; {clean(str(exc), '?', limit=300)}"
    return answer
