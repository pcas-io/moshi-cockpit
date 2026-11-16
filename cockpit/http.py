"""One HTTP exchange for clients that send a bearer token (moshi's API, MCP).

- Redirects are refused, never followed: urllib forwards the Authorization header to whatever origin a
  3xx names, and follows a 302 even for a POST (as a GET with the same headers).
- Bodies are capped at MAX_BODY: a server cannot make us read without end.
- Every failure becomes one HttpFailure with a `kind`, so callers map it to their own error:
  `redirect` (status: the 3xx), `http` (a 4xx/5xx, status and capped body), `unreachable` (status 0:
  refused, DNS, TLS, timeout, the connection dropped before an answer) or `bad_response` (the peer
  answered, but not with usable HTTP or JSON: a garbage status line, a cut-off body, a body over the cap,
  JSON that is invalid or nested deeper than the parser's recursion limit).

Not shadowing the standard library's `http`: the package is always imported as `cockpit.http`, and the
cockpit folder itself is never on sys.path (bin/moshi-cockpit runs with -I and the repository root).
"""

from __future__ import annotations

import http.client
import json
import urllib.error
import urllib.request
from typing import Any, Callable, NamedTuple

Opener = Callable[..., Any]
MAX_BODY = 64 * 1024
DEFAULT_TIMEOUT = 10.0
_CHUNK = 16 * 1024


class HttpFailure(Exception):
    """Never carries a header or a token; `body` is the capped body of an `http` failure."""

    def __init__(self, kind: str, status: int = 0, body: bytes = b"") -> None:
        super().__init__(f"{kind} ({status})")
        self.kind = kind
        self.status = status
        self.body = body


class Reply(NamedTuple):
    status: int
    headers: Any
    body: bytes


class _RefuseRedirects(urllib.request.HTTPRedirectHandler):
    """Turns every 3xx into an HTTPError instead of a second request."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: D102 (urllib API)
        raise urllib.error.HTTPError(req.full_url, code, msg, headers, fp)


def no_redirect_opener() -> Opener:
    """`urlopen` without redirects: the opener for every request that carries a bearer token."""
    return urllib.request.build_opener(_RefuseRedirects()).open


def send(opener: Opener, request: urllib.request.Request, timeout: float = DEFAULT_TIMEOUT) -> Reply:
    """The 2xx reply with its capped body; everything else raises HttpFailure."""
    try:
        return _exchange(opener, request, timeout)
    except HttpFailure:
        raise
    except OSError:  # URLError, refused, DNS, TLS, timeouts, RemoteDisconnected
        raise HttpFailure("unreachable") from None
    except http.client.HTTPException:  # BadStatusLine, IncompleteRead, LineTooLong, ...
        raise HttpFailure("bad_response") from None


def _exchange(opener: Opener, request: urllib.request.Request, timeout: float) -> Reply:
    try:
        with opener(request, timeout=timeout) as response:
            status = int(getattr(response, "status", 200) or 200)
            if 300 <= status < 400:  # an opener that hands a 3xx back instead of raising
                raise HttpFailure("redirect", status)
            return Reply(status, response.headers, read_capped(response, status))
    except urllib.error.HTTPError as exc:
        try:
            if 300 <= exc.code < 400:
                raise HttpFailure("redirect", exc.code) from None
            body = read_capped(exc, exc.code) if exc.fp is not None else b""
        finally:
            exc.close()  # the error holds the open response
        raise HttpFailure("http", exc.code, body) from None


def read_capped(response: Any, status: int) -> bytes:
    """At most MAX_BODY bytes; one more byte than that is a `bad_response`, not a truncated read."""
    chunks = []
    total = 0
    while True:
        chunk = response.read(min(_CHUNK, MAX_BODY + 1 - total))
        if not chunk:
            break
        chunks.append(chunk)
        total += len(chunk)
        if total > MAX_BODY:
            raise HttpFailure("bad_response", status)
    # read(n) does not raise IncompleteRead when the peer closes early; the bytes still owed tell.
    owed = getattr(response, "length", None)
    if isinstance(owed, int) and not isinstance(owed, bool) and owed > 0:
        raise HttpFailure("bad_response", status)
    return b"".join(chunks)


def parse_json(body: bytes, status: int) -> Any:
    """The JSON value of a body, or `bad_response` (also for nesting deeper than the recursion limit)."""
    try:
        return json.loads(body.decode("utf-8", errors="replace"))
    except (ValueError, RecursionError):
        raise HttpFailure("bad_response", status) from None
