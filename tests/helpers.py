"""Shared fakes: a recording `security` runner, a temp HOME and an agent context on top of both."""

from __future__ import annotations

import json
import plistlib
import re
import subprocess
import tempfile
import time
from pathlib import Path
from typing import Dict, List, Tuple

from unittest import mock

from cockpit import launch
from cockpit.agents import AgentContext
from cockpit.backup import BackupStore
from cockpit.secrets.macos import MacKeychain
from cockpit.paths import Paths


class FakeSecurity:
    """Mimics `security` for find/add. Records every call so tests can assert no secret hit argv."""

    def __init__(self, entries: Dict[Tuple[str, str], str] = None, merge_acl: bool = True) -> None:
        self.entries: Dict[Tuple[str, str], str] = dict(entries or {})
        self.strict: Dict[Tuple[str, str], bool] = {}
        self.calls: List[Tuple[List[str], str]] = []
        # Like macOS: `add -U -T ""` on an existing entry keeps (merges) its old trusted apps.
        self.merge_acl = merge_acl
        # service -> how many of the next adds of it fail (stderr only, exit 0, like `security -i`)
        self.fail_adds: Dict[str, int] = {}
        # services whose -T "" macOS would not honour (the ACL check must catch it)
        self.weak: set = set()
        self.delay = 0.0  # seconds per call, to make threads interleave

    def __call__(self, args, input="", **_kwargs):
        self.calls.append((list(args), input))
        if self.delay:
            time.sleep(self.delay)
        if args[:2] == ["security", "find-generic-password"]:
            account = args[args.index("-a") + 1]
            service = args[args.index("-s") + 1]
            value = self.entries.get((service, account))
            if value is None:
                return subprocess.CompletedProcess(args, 44, "", "not found")
            out = value + "\n" if "-w" in args else 'keychain: "login.keychain-db"\n'
            return subprocess.CompletedProcess(args, 0, out, "")
        if args[:2] == ["security", "-i"]:
            match = re.search(r'-a "([^"]+)" -s "([^"]+)".* -w "([^"]+)"', input)
            if not match:
                return subprocess.CompletedProcess(args, 1, "", "error: bad command")
            account, service, secret = match.groups()
            key = (service, account)
            if self.fail_adds.get(service, 0) > 0:
                self.fail_adds[service] -= 1
                return subprocess.CompletedProcess(args, 0, "", "add-generic-password: returned -25308\n")
            if key in self.entries and " -U " not in input:
                # Real `security -i`: exit 0, the failure only on stderr.
                return subprocess.CompletedProcess(args, 0, "", "add-generic-password: returned -25299\n")
            wants_strict = '-T ""' in input and service not in self.weak
            had_apps = key in self.entries and not self.strict.get(key, False)
            self.entries[key] = secret
            self.strict[key] = wants_strict and not (had_apps and self.merge_acl)
            return subprocess.CompletedProcess(args, 0, "", "")
        if args[:2] == ["security", "delete-generic-password"]:
            key = (args[args.index("-s") + 1], args[args.index("-a") + 1])
            if self.entries.pop(key, None) is None:
                return subprocess.CompletedProcess(args, 44, "", "not found")
            self.strict.pop(key, None)
            return subprocess.CompletedProcess(args, 0, "password has been deleted.\n", "")
        if args[:2] == ["security", "dump-keychain"]:
            return subprocess.CompletedProcess(args, 0, self.dump(), "")
        raise AssertionError(f"unexpected command {args}")

    def dump(self) -> str:
        """`security dump-keychain -a` shape: attributes and access lists, never the secrets."""
        blocks = []
        for (service, account), _ in self.entries.items():
            apps = "applications (0):" if self.strict.get((service, account)) else \
                "applications (1):\n            0: /usr/bin/security"
            blocks.append(
                f'keychain: "login.keychain-db"\nclass: "genp"\nattributes:\n    "acct"<blob>="{account}"\n'
                f'    "svce"<blob>="{service}"\naccess: 2 entries\n    entry 0:\n'
                f'        authorizations (6): decrypt derive export_clear export_wrapped mac sign\n'
                f'        don\'t-require-password\n        description: {service}\n        {apps}\n'
                f'    entry 1:\n        authorizations (1): encrypt\n        applications: <null>\n')
        return "".join(blocks)

    def argv_contains(self, secret: str) -> bool:
        return any(secret in part for args, _ in self.calls for part in args)


def temp_paths(test) -> Paths:
    tmp = tempfile.TemporaryDirectory()
    test.addCleanup(tmp.cleanup)
    home = Path(tmp.name)
    (home / ".config").mkdir(mode=0o700)
    apps = home / "Applications"
    apps.mkdir()
    return Paths(home=home, herdr_app_home=home / ".config" / "herdr-app", applications=apps)


MOSHI = "https://moshi.example.test/mcp"
OCELLIO = "https://ocellio.example.test/mcp/"


def configure(paths: Paths, servers=None, agents=(), bind=True) -> None:
    """Writes moshi-cockpit's config and agent list straight into a temp HOME (no backup store), owner-only
    like the real ones. With bind=True each agent's tokens are bound to the configured servers' origins."""
    import json as _json
    import os as _os
    from cockpit.config import origin
    paths.mesh_config.parent.mkdir(parents=True, exist_ok=True)
    servers = dict(servers if servers is not None else {"moshi": MOSHI, "ocellio": OCELLIO})
    data = {"servers": servers}
    paths.mesh_config.write_text(_json.dumps(data))
    items = []
    for agent in agents:
        item = dict(agent)
        if bind and "bound" not in item:
            item["bound"] = {name: origin(url) for name, url in servers.items()}
        items.append(item)
    paths.agents_file.write_text(_json.dumps({"agents": items}))
    for f in (paths.mesh_config, paths.agents_file):
        _os.chmod(f, 0o600)


# herdr's own `main` layout, as Herdr writes it at its first start.
MAIN_LAYOUT = {"id": "root", "type": "row", "children": [
    {"id": "sidebar", "type": "column", "children": [{"id": "agents", "content": {"mod": "agents"}}]},
    {"id": "work", "type": "column", "children": [{"id": "main", "content": {"tab": "active"}}]}]}


def herdr_app(paths: Paths, version: str = "0.1.13", ran_before: bool = True) -> None:
    """A Herdr.app as the vendor installs it, in the temp Applications folder. ran_before writes Herdr's own
    main layout, so the installer has no reason to start it (no test ever opens an app)."""
    contents = paths.herdr_app_bundle / "Contents"
    contents.mkdir(parents=True, exist_ok=True)
    with (contents / "Info.plist").open("wb") as handle:
        plistlib.dump({"CFBundleShortVersionString": version}, handle)
    if ran_before:
        layouts = paths.herdr_app_home / "layouts"
        layouts.mkdir(parents=True, exist_ok=True)
        (layouts / "main.json").write_text(json.dumps(MAIN_LAYOUT))


def bind_non_agent(paths: Paths, url: str = MOSHI, identity: str = "device") -> None:
    """Binds a non-agent identity (device, register) in an existing test config, as `connect` does."""
    import json as _json
    from cockpit.config import origin
    data = _json.loads(paths.mesh_config.read_text())
    data.setdefault("bound", {})[identity] = origin(url)
    paths.mesh_config.write_text(_json.dumps(data))


OWN, FOREIGN = "bt_OWN_token_1234567890", "bt_FOREIGN_token_123456"


def context(test, entries=None):
    paths = temp_paths(test)
    fake = FakeSecurity(entries or {})
    return AgentContext(paths=paths, backup=BackupStore(paths.state), keychain=MacKeychain(fake)), fake


def start(ctx, name, extra=(), environ=None):
    seen = {}
    with mock.patch.object(launch.shutil, "which", return_value="/usr/bin/true"):
        launch.run_agent(ctx.paths, ctx.keychain, name, list(extra), environ=environ or {"PATH": "/usr/bin"},
                         execve=lambda path, argv, env: seen.update(argv=argv, env=env))
    return seen
