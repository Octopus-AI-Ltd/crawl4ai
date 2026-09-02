"""The crawler's front door.

Written 2026-09-02, after confirming from a laptop — no credentials of any kind —
that https://crawl4ai-production-*.up.railway.app/monitor/health returned the
service's live internals. Every crawl and every diagnostic was open to anyone who
knew the URL.
"""

import asyncio
import json
import os
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "deploy" / "docker"))

from shared_token import (  # noqa: E402
    OPEN_PATHS,
    TOKEN_ENV_VAR,
    SharedTokenMiddleware,
    denial_reason,
    install,
    presented_token,
    read_expected_token,
)

SECRET = "s3cret-token-value"


class ReadExpectedTokenTests(unittest.TestCase):
    def test_reads_the_variable(self):
        self.assertEqual(read_expected_token({TOKEN_ENV_VAR: SECRET}), SECRET)

    def test_absent_or_empty_is_no_token(self):
        self.assertEqual(read_expected_token({}), "")
        self.assertEqual(read_expected_token({TOKEN_ENV_VAR: ""}), "")

    def test_whitespace_is_not_a_token(self):
        # 🔴 A variable set to a space or a stray newline must not read as a lock
        # that is on. It would look configured and protect nothing.
        self.assertEqual(read_expected_token({TOKEN_ENV_VAR: "   "}), "")
        self.assertEqual(read_expected_token({TOKEN_ENV_VAR: "\n"}), "")

    def test_trims_a_pasted_value(self):
        self.assertEqual(read_expected_token({TOKEN_ENV_VAR: f"  {SECRET}\n"}), SECRET)


class PresentedTokenTests(unittest.TestCase):
    def test_reads_a_bearer_header(self):
        self.assertEqual(presented_token(f"Bearer {SECRET}"), SECRET)

    def test_scheme_is_case_insensitive(self):
        for header in (f"bearer {SECRET}", f"BEARER {SECRET}", f"BeArEr {SECRET}"):
            self.assertEqual(presented_token(header), SECRET, header)

    def test_ignores_another_scheme(self):
        self.assertEqual(presented_token(f"Basic {SECRET}"), "")
        self.assertEqual(presented_token(SECRET), "")

    def test_nothing_at_all(self):
        self.assertEqual(presented_token(None), "")
        self.assertEqual(presented_token(""), "")
        self.assertEqual(presented_token("Bearer"), "")
        self.assertEqual(presented_token("Bearer    "), "")


class DenialReasonTests(unittest.TestCase):
    def test_lets_the_right_token_through(self):
        self.assertIsNone(denial_reason("/crawl", f"Bearer {SECRET}", SECRET))

    def test_turns_away_no_token(self):
        self.assertEqual(denial_reason("/crawl", None, SECRET), "Missing bearer token")

    def test_turns_away_a_wrong_token(self):
        self.assertEqual(denial_reason("/crawl", "Bearer nope", SECRET), "Invalid bearer token")

    def test_the_diagnostics_are_locked_too(self):
        # 🔴 This is the endpoint that was actually reachable from outside. It is
        # not behind the route dependency the crawl endpoints use, so only a
        # middleware covers it.
        self.assertEqual(denial_reason("/monitor/health", None, SECRET), "Missing bearer token")
        self.assertEqual(denial_reason("/monitor/ws", None, SECRET), "Missing bearer token")
        self.assertEqual(denial_reason("/metrics", None, SECRET), "Missing bearer token")
        self.assertEqual(denial_reason("/", None, SECRET), "Missing bearer token")

    def test_the_healthcheck_stays_open(self):
        # 🔴🔴 The container healthcheck runs every 30s and sends no header. Lock
        # this and the platform kills a service that is working perfectly.
        self.assertIsNone(denial_reason("/health", None, SECRET))
        self.assertIn("/health", OPEN_PATHS)

    def test_no_secret_configured_means_the_door_is_open(self):
        # Deliberate: it is what lets the caller be given the token first and the
        # lock be turned second, with no window where crawling is broken. It is
        # also why `install` shouts about it in the log.
        self.assertIsNone(denial_reason("/crawl", None, ""))
        self.assertIsNone(denial_reason("/monitor/health", None, ""))

    def test_a_near_miss_is_still_a_miss(self):
        self.assertIsNone(denial_reason("/crawl", f"Bearer {SECRET} ", SECRET), "a trailing space is trimmed, not a different token")
        self.assertEqual(denial_reason("/crawl", f"Bearer {SECRET}x", SECRET), "Invalid bearer token")
        self.assertEqual(denial_reason("/crawl", f"Bearer {SECRET[:-1]}", SECRET), "Invalid bearer token")
        self.assertEqual(denial_reason("/crawl", f"Bearer {SECRET.upper()}", SECRET), "Invalid bearer token")


class _Recorder:
    """Stands in for the app behind the middleware."""

    def __init__(self):
        self.called = False

    async def __call__(self, scope, receive, send):
        self.called = True
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b"ok"})


def _run(middleware, scope):
    sent = []

    async def send(message):
        sent.append(message)

    async def receive():
        return {"type": "http.request"}

    asyncio.run(middleware(scope, receive, send))
    return sent


def _http(path, auth=None, method="POST"):
    headers = [(b"content-type", b"application/json")]
    if auth is not None:
        headers.append((b"Authorization", auth.encode()))
    return {"type": "http", "method": method, "path": path, "headers": headers}


class MiddlewareTests(unittest.TestCase):
    def test_passes_an_authorised_request_to_the_app(self):
        app = _Recorder()
        sent = _run(SharedTokenMiddleware(app, expected=SECRET), _http("/crawl", f"Bearer {SECRET}"))
        self.assertTrue(app.called)
        self.assertEqual(sent[0]["status"], 200)

    def test_stops_an_unauthorised_request_before_the_app(self):
        app = _Recorder()
        sent = _run(SharedTokenMiddleware(app, expected=SECRET), _http("/crawl"))
        self.assertFalse(app.called, "the request must not reach the crawler at all")
        self.assertEqual(sent[0]["status"], 401)
        self.assertEqual(json.loads(sent[1]["body"])["detail"], "Missing bearer token")

    def test_the_401_says_how_to_authenticate(self):
        sent = _run(SharedTokenMiddleware(_Recorder(), expected=SECRET), _http("/crawl"))
        headers = {k.lower(): v for k, v in sent[0]["headers"]}
        self.assertEqual(headers[b"www-authenticate"], b"Bearer")

    def test_header_name_is_matched_case_insensitively(self):
        # HTTP/2 lowercases header names; HTTP/1.1 clients often do not.
        app = _Recorder()
        scope = {"type": "http", "method": "POST", "path": "/crawl",
                 "headers": [(b"AUTHORIZATION", f"Bearer {SECRET}".encode())]}
        _run(SharedTokenMiddleware(app, expected=SECRET), scope)
        self.assertTrue(app.called)

    def test_healthcheck_reaches_the_app_without_a_token(self):
        app = _Recorder()
        sent = _run(SharedTokenMiddleware(app, expected=SECRET), _http("/health", method="GET"))
        self.assertTrue(app.called)
        self.assertEqual(sent[0]["status"], 200)

    def test_closes_an_unauthorised_websocket_instead_of_replying_401(self):
        # 🔴 A websocket has no status code. Sending an HTTP response on a
        # websocket scope raises, so the guard would crash rather than refuse.
        app = _Recorder()
        scope = {"type": "websocket", "path": "/monitor/ws", "headers": []}
        sent = _run(SharedTokenMiddleware(app, expected=SECRET), scope)
        self.assertFalse(app.called)
        self.assertEqual(sent, [{"type": "websocket.close", "code": 1008}])

    def test_lets_an_authorised_websocket_through(self):
        app = _Recorder()
        scope = {"type": "websocket", "path": "/monitor/ws",
                 "headers": [(b"authorization", f"Bearer {SECRET}".encode())]}

        async def ws_app(scope, receive, send):
            app.called = True

        _run(SharedTokenMiddleware(ws_app, expected=SECRET), scope)
        self.assertTrue(app.called)

    def test_lifespan_and_other_scopes_pass_straight_through(self):
        # 🔴 Startup runs as an ASGI 'lifespan' scope with no path and no headers.
        # Treating it like a request would stop the service booting at all.
        seen = {}

        async def lifespan_app(scope, receive, send):
            seen["type"] = scope["type"]

        asyncio.run(SharedTokenMiddleware(lifespan_app, expected=SECRET)(
            {"type": "lifespan"}, lambda: None, lambda m: None
        ))
        self.assertEqual(seen["type"], "lifespan")

    def test_open_door_when_no_secret_is_configured(self):
        app = _Recorder()
        sent = _run(SharedTokenMiddleware(app, expected=""), _http("/crawl"))
        self.assertTrue(app.called)
        self.assertEqual(sent[0]["status"], 200)


class _FakeApp:
    def __init__(self):
        self.middleware = []

    def add_middleware(self, cls, **kwargs):
        self.middleware.append((cls, kwargs))


class InstallTests(unittest.TestCase):
    def setUp(self):
        self._saved = os.environ.get(TOKEN_ENV_VAR)
        os.environ.pop(TOKEN_ENV_VAR, None)

    def tearDown(self):
        os.environ.pop(TOKEN_ENV_VAR, None)
        if self._saved is not None:
            os.environ[TOKEN_ENV_VAR] = self._saved

    def test_fits_the_lock_and_says_so(self):
        os.environ[TOKEN_ENV_VAR] = SECRET
        app = _FakeApp()
        with self.assertLogs("shared_token", level="INFO") as logs:
            self.assertTrue(install(app))
        self.assertEqual(len(app.middleware), 1)
        self.assertIs(app.middleware[0][0], SharedTokenMiddleware)
        self.assertEqual(app.middleware[0][1]["expected"], SECRET)
        self.assertIn("ON", "".join(logs.output))

    def test_says_loudly_when_there_is_no_lock(self):
        # ⭐ The whole reason this logs. A lock that is quietly off looks exactly
        # like one that is on, and nobody finds out until it matters.
        app = _FakeApp()
        with self.assertLogs("shared_token", level="WARNING") as logs:
            self.assertFalse(install(app))
        self.assertEqual(app.middleware, [])
        joined = "".join(logs.output)
        self.assertIn("OFF", joined)
        self.assertIn(TOKEN_ENV_VAR, joined)

    def test_a_blank_variable_does_not_count_as_configured(self):
        os.environ[TOKEN_ENV_VAR] = "   "
        app = _FakeApp()
        with self.assertLogs("shared_token", level="WARNING"):
            self.assertFalse(install(app))
        self.assertEqual(app.middleware, [])


if __name__ == "__main__":
    unittest.main()
