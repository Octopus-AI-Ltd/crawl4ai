"""A shared secret on the crawler's front door.

This service is reachable from the public internet. Until this existed, anyone who
found the URL could make it crawl anything at our expense and read every diagnostic
it holds — no password, no rate limit that matters, nothing.

Only octopus-be ever calls it, so one shared secret is the right size of lock. The
JWT machinery already in `auth.py` is built for a public multi-tenant service: it
would mean minting and refreshing hour-long tokens for a single caller, which is
more moving parts to fail at 3am than the thing it protects.

Set ``CRAWL4AI_API_TOKEN`` to turn the lock on. Leave it unset and the door stays
open — that is deliberate, and it is what lets the token be given to the caller
first and the lock turned second, with no window where crawling is broken.
"""

import hmac
import json
import logging
import os
from typing import Iterable, Optional, Tuple

logger = logging.getLogger(__name__)

TOKEN_ENV_VAR = "CRAWL4AI_API_TOKEN"

# The container's own healthcheck hits this every 30 seconds and cannot carry a
# header. Locking it would have the platform kill a service that is working fine.
OPEN_PATHS = frozenset({"/health"})

_BEARER = "bearer "


def read_expected_token(env: Optional[dict] = None) -> str:
    """The configured secret, or '' when there is none. Whitespace is not a token."""
    source = os.environ if env is None else env
    return (source.get(TOKEN_ENV_VAR) or "").strip()


def presented_token(auth_header: Optional[str]) -> str:
    """The token out of an Authorization header, or '' if there isn't one."""
    if not auth_header:
        return ""
    header = auth_header.strip()
    if header[: len(_BEARER)].lower() != _BEARER:
        return ""
    return header[len(_BEARER) :].strip()


def denial_reason(path: str, auth_header: Optional[str], expected: str) -> Optional[str]:
    """None when the request may proceed, otherwise why it may not."""
    if not expected:
        # No secret configured. Refusing everything here would take the whole
        # service down the moment someone cleared the variable by accident.
        return None
    if path in OPEN_PATHS:
        return None
    presented = presented_token(auth_header)
    if not presented:
        return "Missing bearer token"
    # Constant time, so a wrong token cannot be guessed a character at a time.
    if not hmac.compare_digest(presented, expected):
        return "Invalid bearer token"
    return None


def _header(raw_headers: Iterable[Tuple[bytes, bytes]], name: bytes) -> Optional[str]:
    for key, value in raw_headers:
        if key.lower() == name:
            return value.decode("latin-1")
    return None


class SharedTokenMiddleware:
    """Raw ASGI, not BaseHTTPMiddleware, so it covers websockets as well as HTTP.

    ⚠️ `@app.middleware("http")` would leave `/monitor/ws` — a live feed of what the
    crawler is doing — open to anyone.
    """

    def __init__(self, app, expected: Optional[str] = None):
        self.app = app
        self.expected = read_expected_token() if expected is None else expected

    async def __call__(self, scope, receive, send):
        if scope["type"] not in ("http", "websocket"):
            await self.app(scope, receive, send)
            return

        reason = denial_reason(
            scope.get("path", ""),
            _header(scope.get("headers") or (), b"authorization"),
            self.expected,
        )
        if reason is None:
            await self.app(scope, receive, send)
            return

        logger.warning(
            "🔒 Refused %s %s: %s",
            scope.get("method") or scope["type"],
            scope.get("path"),
            reason,
        )
        if scope["type"] == "websocket":
            await send({"type": "websocket.close", "code": 1008})
            return
        await send(
            {
                "type": "http.response.start",
                "status": 401,
                "headers": [
                    (b"content-type", b"application/json"),
                    (b"www-authenticate", b"Bearer"),
                ],
            }
        )
        await send({"type": "http.response.body", "body": json.dumps({"detail": reason}).encode()})


def install(app) -> bool:
    """Fit the lock if a secret is configured, and say plainly which state we are in.

    ⭐ The log line is the point. A lock that is quietly off looks exactly like a
    lock that is on, and nobody finds out until it matters.
    """
    expected = read_expected_token()
    if expected:
        app.add_middleware(SharedTokenMiddleware, expected=expected)
        logger.info(
            "🔒 Shared-token auth ON (%s is set). Open without a token: %s",
            TOKEN_ENV_VAR,
            ", ".join(sorted(OPEN_PATHS)),
        )
    else:
        logger.warning(
            "🔓 Shared-token auth OFF — %s is not set, so every endpoint is open to "
            "anyone who knows this URL",
            TOKEN_ENV_VAR,
        )
    return bool(expected)
