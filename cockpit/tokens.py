"""Every moshi token that leaves the keychain for a network call goes through here: the token is read only if the configured moshi origin is the one it was bound to.

Agents keep their binding in agents.json (`bound`); the identities that are not agents, the human's
`cockpit` identity, the moshi admin token and the registration token, keep theirs in config.json
(`bound`), owner-only like it. Bindings are set by explicit acts only: `bot add`, `bot admin-token`,
`bot register-token set`, the one-time migration.
"""

from __future__ import annotations

import json
from typing import Optional

from cockpit.backup import BackupStore
from cockpit.config import _read, origin
from cockpit.secrets import SecretStore
from cockpit.paths import COCKPIT_IDENTITY, Paths, keychain_service

ADMIN = "admin"  # the binding key for the admin token (service moshi-admin, account admin)
REGISTER = "register"  # the binding key for the registration token (service moshi-register, account register)
COCKPIT_OCELLIO = "cockpit-ocellio"  # the binding key for the person's ocellio token that `connect` stores
NON_AGENT = (COCKPIT_IDENTITY, ADMIN, REGISTER, COCKPIT_OCELLIO)
HINTS = {ADMIN: "moshi-cockpit bot admin-token", REGISTER: "moshi-cockpit bot register-token set"}


def bound_origin(paths: Paths, identity: str) -> Optional[str]:
    if identity in NON_AGENT:
        bound = _read(paths).get("bound") or {}
        value = bound.get(identity) if isinstance(bound, dict) else None
        return value if isinstance(value, str) else None
    from cockpit.registry import find
    return find(paths, identity, enforce=False).bound.get("moshi")


def check(paths: Paths, identity: str, url: str) -> None:
    expected, actual = bound_origin(paths, identity), origin(url)
    if expected != actual:
        hint = HINTS.get(identity, f"moshi-cockpit bot add {identity}")
        if expected is None:
            raise ValueError(f"moshi-Token von '{identity}' ist noch an keinen Server gebunden; kein Token gesendet. "
                             f"Binden: {hint}")
        raise ValueError(f"moshi-Token von '{identity}' ist an {expected} gebunden, die Config zeigt auf {actual}; "
                         f"kein Token gesendet. Absichtlich umgezogen? {hint} --rebind")


def moshi_token(paths: Paths, keychain: SecretStore, identity: str, url: str) -> str:
    """The identity's moshi token, only for the origin it is bound to (fail-closed otherwise)."""
    check(paths, identity, url)
    if identity == ADMIN:
        from cockpit.admin import ADMIN_ACCOUNT, ADMIN_SERVICE
        return keychain.read(ADMIN_SERVICE, ADMIN_ACCOUNT)
    if identity == REGISTER:
        from cockpit.register import REGISTER_ACCOUNT, REGISTER_SERVICE
        return keychain.read(REGISTER_SERVICE, REGISTER_ACCOUNT)
    return keychain.read(keychain_service("moshi", identity), identity)


def bind_identity(paths: Paths, backup: BackupStore, identity: str, url: str, rebind: bool = False) -> str:
    """Binds a non-agent identity (cockpit, admin, register) to the origin of `url`."""
    if identity not in NON_AGENT:
        raise ValueError(f"'{identity}' ist ein Agent: Bindung über die Agent-Liste (bot add {identity})")
    data = _read(paths)
    bound = data.get("bound") if isinstance(data.get("bound"), dict) else {}
    value = origin(url)
    if bound.get(identity) not in (None, value) and not rebind:
        raise ValueError(f"'{identity}' ist an {bound[identity]} gebunden, die Config zeigt auf {value}. "
                         "Absichtlich umgezogen? Mit --rebind")
    bound[identity] = value
    data["bound"] = bound
    backup.write_text(paths.mesh_config, json.dumps(data, indent=2) + "\n", mode=0o600)
    return value


def unbind_identity(paths: Paths, backup: BackupStore, identity: str) -> bool:
    """Drops a non-agent binding (used when its token is removed). True if there was one."""
    if identity not in NON_AGENT:
        raise ValueError(f"'{identity}' ist ein Agent: Bindung über die Agent-Liste")
    data = _read(paths)
    bound = data.get("bound") if isinstance(data.get("bound"), dict) else {}
    if identity not in bound:
        return False
    del bound[identity]
    data["bound"] = bound
    backup.write_text(paths.mesh_config, json.dumps(data, indent=2) + "\n", mode=0o600)
    return True
