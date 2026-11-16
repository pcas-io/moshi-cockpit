"""Every token that leaves the keychain for a network call goes through here: the token is read only if the configured server's origin is the one it was bound to.

Agents keep their binding in agents.json (`bound`). The tokens that belong to this installation rather than
to an agent keep theirs in config.json (`bound`), owner-only like it:

- `device`: the device key (service `moshi-device`, account `device`),
- `register`: the registration token (service `moshi-register`, account `register`),
- `ocellio-default`: the person's ocellio token (service `ocellio-default`, account `default`).

Bindings are set by explicit acts only: `connect` and the one-time migration.
"""

from __future__ import annotations

from typing import Optional

from cockpit.backup import BackupStore
from cockpit.config import origin, setting, update
from cockpit.secrets import SecretStore
from cockpit.paths import Paths, keychain_service

DEVICE = "device"
REGISTER = "register"
OCELLIO_DEFAULT = "ocellio-default"
NON_AGENT = (DEVICE, REGISTER, OCELLIO_DEFAULT)
CONNECT = "moshi-cockpit connect"


def bound_origin(paths: Paths, identity: str) -> Optional[str]:
    if identity in NON_AGENT:
        bound = setting(paths, "bound")
        value = bound.get(identity) if isinstance(bound, dict) else None
        return value if isinstance(value, str) else None
    from cockpit.registry import find
    return find(paths, identity, enforce=False).bound.get("moshi")


def check(paths: Paths, identity: str, url: str) -> None:
    expected, actual = bound_origin(paths, identity), origin(url)
    if expected == actual:
        return
    hint = f" Connect this Mac: {CONNECT}" if identity in NON_AGENT else ""
    if expected is None:
        raise ValueError(f"The token of '{identity}' is not bound to a server yet; no token sent.{hint}")
    raise ValueError(f"The token of '{identity}' is bound to {expected}, but the config points to {actual}; "
                     f"no token sent.{hint}")


def moshi_token(paths: Paths, keychain: SecretStore, identity: str, url: str) -> str:
    """The identity's moshi token, only for the origin it is bound to (fail-closed otherwise)."""
    if identity == OCELLIO_DEFAULT:
        raise ValueError(f"'{identity}' is an ocellio token, not a moshi token")
    check(paths, identity, url)
    return keychain.read(keychain_service("moshi", identity), identity)


def bind_identity(paths: Paths, backup: BackupStore, identity: str, url: str, rebind: bool = False) -> str:
    """Binds a token that is not an agent's (device, register, ocellio-default) to the origin of `url`."""
    if identity not in NON_AGENT:
        raise ValueError(f"'{identity}' is an agent: its binding lives in the agent list")
    bound = _bindings(paths)
    value = origin(url)
    if bound.get(identity) not in (None, value) and not rebind:
        raise ValueError(f"'{identity}' is bound to {bound[identity]}, but the config points to {value}; "
                         "the binding stays as it is")
    bound[identity] = value
    update(paths, backup, "bound", bound)
    return value


def unbind_identity(paths: Paths, backup: BackupStore, identity: str) -> bool:
    """Drops a binding that is not an agent's (used when its token is removed). True if there was one."""
    if identity not in NON_AGENT:
        raise ValueError(f"'{identity}' is an agent: its binding lives in the agent list")
    bound = _bindings(paths)
    if identity not in bound:
        return False
    del bound[identity]
    update(paths, backup, "bound", bound)
    return True


def _bindings(paths: Paths) -> dict:
    bound = setting(paths, "bound")
    return dict(bound) if isinstance(bound, dict) else {}
