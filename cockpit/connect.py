"""`moshi-cockpit connect`: this Herdr becomes an installation in moshi with one approval in the browser.

Herdr's first dialog opens this in a terminal tab, because a mod never sees a token. moshi's device flow:
start it, open the approval page, poll until the person approved or denied it there. The grant is a
device key (`moshi-device`, read-only: installation status) and a registration token (`moshi-register`,
strict: creates and loads agents) that expires at `register_expires_at` (config.json). With a device key
already here, the same flow renews only the registration token; the installation and its agent tokens
stay. A device key moshi no longer knows (removed in the dashboard) gives a new installation.

Until ocellio has an approval of its own, the person's ocellio token is asked for once, hidden.
"""

from __future__ import annotations

import getpass
import platform
import socket
import subprocess
import time
import webbrowser
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Callable, Optional, Tuple, Union
from urllib.parse import urlsplit

from cockpit.agents import AgentContext
from cockpit.config import load_config, origin, require, setting, update
from cockpit.moshi_api import DeviceGrant, DeviceStart, MoshiApi, MoshiError, parse_timestamp
from cockpit.paths import Paths, keychain_service
from cockpit.secrets import KeychainError, SecretStore, validate_token
from cockpit.text import clean
from cockpit.tokens import DEVICE, OCELLIO_DEFAULT, REGISTER, bind_identity, bound_origin, moshi_token

DEVICE_ENTRY = (keychain_service("moshi", DEVICE), DEVICE)
REGISTER_ENTRY = (keychain_service("moshi", REGISTER), REGISTER)
OCELLIO_ENTRY = (keychain_service("ocellio", "default"), "default")
EXPIRES_KEY = "register_expires_at"
SLOW_DOWN = 5  # seconds moshi adds to the interval with every slow_down

DONE = 'Done. Back in Herdr, add your first agent with "+ Agent".'
EXPIRED = "The code expired. Run connect again."
DENIED = "Connection was denied in moshi."
LOST = "Lost the connection to moshi while waiting for your approval. Check the network and run connect again."
UNREACHABLE = "Could not reach moshi at {origin}. Check the network and run connect again."
SERVER_ERROR = "moshi answered with an error ({status}). Try again later."
ANOTHER = ("This Herdr is connected to another moshi. Run moshi-cockpit uninstall or remove the old connection in "
           "the Keychain, then connect again.")
UNKNOWN = "This Herdr's earlier connection is unknown here; connecting it again."
ALREADY = "This Herdr is connected to moshi already."
OCELLIO_PROMPT = "Your ocellio token (input hidden, stays in the Keychain): "
NO_OCELLIO = "No valid ocellio token entered, nothing stored. Run connect again to add it."


@dataclass(frozen=True)
class ConnectIO:
    ask_secret: Callable[[str], str] = getpass.getpass
    say: Callable[[str], None] = print


def open_browser(url: str) -> bool:
    """Opens the approval page. https only: a URL from the server never opens a file or an app."""
    if urlsplit(url).scheme != "https":
        return False
    if platform.system() == "Darwin":
        try:
            done = subprocess.run(["/usr/bin/open", url], check=False, stdout=subprocess.DEVNULL,
                                  stderr=subprocess.DEVNULL)
        except OSError:
            return False
        return done.returncode == 0
    return webbrowser.open(url)


def needs_connect(paths: Paths, config, store: SecretStore) -> bool:
    """moshi is set up, but this Herdr is no installation there yet (no device key)."""
    return config.has("moshi") and not store.exists(*DEVICE_ENTRY)


def register_expires_at(paths: Paths) -> Optional[str]:
    value = setting(paths, EXPIRES_KEY)
    return value if isinstance(value, str) else None


def approval_valid(paths: Paths, store: SecretStore, now: Optional[datetime] = None) -> bool:
    """A registration token is stored and `register_expires_at` lies after `now` (a naive one counts as UTC)."""
    if not store.exists(*REGISTER_ENTRY):
        return False
    try:
        expires = parse_timestamp(register_expires_at(paths) or "")
    except ValueError:
        return False
    now = now or datetime.now(timezone.utc)
    return expires > (now.replace(tzinfo=timezone.utc) if now.tzinfo is None else now)


def _refuse_another_moshi(paths: Paths, url: str) -> None:
    """A device key or registration token of another moshi is never sent, and never replaced unasked."""
    if any(bound_origin(paths, identity) not in (None, origin(url)) for identity in (DEVICE, REGISTER)):
        raise ValueError(ANOTHER)


def _transient(exc: MoshiError) -> bool:
    """No answer, or one from a proxy or a restarting server: worth asking again."""
    return exc.reason == "unreachable" or exc.status >= 500


def _start(ctx: AgentContext, io: ConnectIO, api: MoshiApi, url: str, label: str) -> Tuple[DeviceStart, bool]:
    """A renewal with the device key when there is one (True); a new installation when there is none, it is
    bound to no server (never sent then), or moshi no longer knows it."""
    if ctx.keychain.exists(*DEVICE_ENTRY) and bound_origin(ctx.paths, DEVICE) is None:
        io.say(UNKNOWN)
    elif ctx.keychain.exists(*DEVICE_ENTRY):
        try:
            return api.device_authorize(label, device_key=moshi_token(ctx.paths, ctx.keychain, DEVICE, url)), True
        except MoshiError as exc:
            if exc.reason != "invalid_device_key":
                raise
            io.say("moshi no longer knows this Herdr; it is connected as a new one.")
    return api.device_authorize(label), False


def _wait(api: MoshiApi, start: DeviceStart, sleep: Callable[[float], None]) -> Union[DeviceGrant, str]:
    """Polls at moshi's interval until a grant; otherwise the sentence that ends the attempt."""
    interval, waited, lost = start.interval, 0, False
    while waited + interval <= start.expires_in:
        sleep(interval)
        waited += interval
        try:
            answer = api.device_token(start.device_code)
        except MoshiError as exc:
            if not _transient(exc):
                raise
            lost = True
            continue
        lost = False
        if isinstance(answer, DeviceGrant):
            return answer
        if answer == "slow_down":
            interval += SLOW_DOWN
        elif answer == "expired_token":
            return EXPIRED
        elif answer == "access_denied":
            return DENIED
    return LOST if lost else EXPIRED


def _store(ctx: AgentContext, url: str, grant: DeviceGrant, renewal: bool) -> None:
    """Checked first, bound before written: device key plain, registration token strict, then its expiry."""
    if grant.device_key is None and not renewal:
        raise MoshiError("bad_response", 200)  # a new installation always comes with its key
    parse_timestamp(grant.registration_expires_at)
    validate_token(grant.registration_token)
    if grant.device_key is not None:
        validate_token(grant.device_key)
    bind_identity(ctx.paths, ctx.backup, REGISTER, url)
    if grant.device_key is not None:  # None on a renewal: the installation keeps its key
        bind_identity(ctx.paths, ctx.backup, DEVICE, url)
        ctx.keychain.write(*DEVICE_ENTRY, grant.device_key)
    ctx.keychain.write(*REGISTER_ENTRY, grant.registration_token, strict=True)  # macOS asks on every read
    update(ctx.paths, ctx.backup, EXPIRES_KEY, grant.registration_expires_at)


def _ocellio_missing(ctx: AgentContext, config) -> bool:
    return config.has("ocellio") and not ctx.keychain.exists(*OCELLIO_ENTRY)


def _ocellio(ctx: AgentContext, io: ConnectIO, config) -> int:
    """Asked once, hidden, and only while missing."""
    if not _ocellio_missing(ctx, config):
        return 0
    token = io.ask_secret(OCELLIO_PROMPT).strip()
    try:
        validate_token(token)
    except KeychainError:
        io.say(NO_OCELLIO)
        return 1
    bind_identity(ctx.paths, ctx.backup, OCELLIO_DEFAULT, config.servers["ocellio"])
    ctx.keychain.write(*OCELLIO_ENTRY, token)
    return 0


def _same_origin(link: str, url: str) -> bool:
    try:
        return origin(link) == origin(url)
    except ValueError:  # a malformed port
        return False


def _connect_moshi(ctx: AgentContext, io: ConnectIO, api: MoshiApi, url: str, opener: Callable[[str], bool],
                   sleep: Callable[[float], None], label: str) -> int:
    io.say("Connecting this Herdr to moshi.")
    try:
        start, renewal = _start(ctx, io, api, url, label)
    except MoshiError as exc:
        if exc.reason == "unreachable":
            io.say(UNREACHABLE.format(origin=origin(url)))
        elif exc.status >= 500:
            io.say(SERVER_ERROR.format(status=exc.status))
        else:
            raise
        return 1
    link, code = clean(start.verification_uri), clean(start.user_code)
    if _same_origin(start.verification_uri_complete, url):
        io.say(f"Confirm in your browser. If it opens on another device, go to {link} and enter {code}.")
        opener(start.verification_uri_complete)
    else:  # a page of another server is never opened unasked
        io.say(f"Open {link} in your browser and enter {code}.")
    outcome = _wait(api, start, sleep)
    if not isinstance(outcome, DeviceGrant):
        io.say(outcome)
        return 1
    _store(ctx, url, outcome, renewal)
    io.say(f'Connected as "{clean(outcome.installation_label, "?", limit=64)}".')
    return 0


def run(ctx: AgentContext, io: ConnectIO, api: MoshiApi, opener: Callable[[str], bool] = open_browser,
        sleep: Callable[[float], None] = time.sleep, label: Optional[str] = None) -> int:
    """0 once this Herdr is connected; 1 with one sentence when the attempt ended. `label` defaults to the
    host name. Errors that are not part of the flow (keychain, config, moshi's refusals) are raised.
    With a valid approval and only the ocellio token missing, there is no browser step."""
    config = load_config(ctx.paths)
    url = require(config, "moshi")
    _refuse_another_moshi(ctx.paths, url)
    if (_ocellio_missing(ctx, config) and ctx.keychain.exists(*DEVICE_ENTRY)
            and bound_origin(ctx.paths, DEVICE) is not None and approval_valid(ctx.paths, ctx.keychain)):
        io.say(ALREADY)
    elif _connect_moshi(ctx, io, api, url, opener, sleep, f"Herdr on {label or socket.gethostname()}"):
        return 1
    if _ocellio(ctx, io, config):
        return 1
    io.say(DONE)
    return 0
