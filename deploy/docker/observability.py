"""Crash reporting for the crawler. OCTOPUS ADDITION — not from upstream crawl4ai.

⭐ This service is the one that actually crawls for Octopus. `CRAWLER_SERVICE_URL` on
`octopus-api` points at `crawl4ai.railway.internal:11235`, and the `crawl_<hex>` task ids in
octopus-be's own Sentry warnings are this server's. There is a separate
`Octopus-AI-Ltd/octopus-crawler-service` repo of a similar shape which is NOT deployed
anywhere — do not confuse the two, and do not add reporting there expecting it to ship.

Until now this service reported nothing. Knowledge is what customers buy an assistant for and
this is where it comes from, so the silence was being read as "crawling is fine" when it only
ever meant "crawling is invisible".

⚠️ Read this before changing anything here
------------------------------------------
Installing the SDK alone is NOT enough, and on its own would give a false sense of coverage.
Sentry reports exceptions that ESCAPE a route, and the two failures that matter most here do
not escape anything:

1. **The crawl job** (`api.py`) catches every exception, writes `status: FAILED` and an error
   string into Redis, fires the "failed" webhook, and moves on. Nothing is raised and nothing
   is even logged. That is the path octopus-be uses for a real crawl, so a crawl that dies
   takes its cause with it.
2. **The seed job** (`api.py`) does exactly the same for sitemap seeding.
3. **`/crawl`** turns "every result failed" into `HTTPException(500, ...)`. Sentry ignores
   HTTPException by design — it looks like a deliberate response rather than a crash — so that
   one needs reporting too.

Those three call `report_exception()`. Everything else is covered by the logging hook below.

`event_level=ERROR` is safe in this repo, unlike octopus-ai / octopus-rag / octopus-ingestion
where it had to be off: none of the 39 `logger.error` calls here interpolate a traceback into
the message, so they group properly, and nine of them already pass `exc_info=True` and arrive
as fully structured exceptions.

Deliberately left off
---------------------
- `send_default_pii` stays False (the SDK default). Nothing here needs a requester identity.
- Tracing defaults to OFF. The organisation was already over its span allowance and had to be
  sampled down (be#681). Set SENTRY_TRACES_SAMPLE_RATE if spans are ever wanted here.

⭐ The tag that earns its place is the site. Cloudflare challenges and outright IP blocks hit
per-host, and "which site?" is the first question every time.

With no SENTRY_DSN set this whole module is inert, so upstream behaviour is unchanged and
anyone running this fork without a DSN sees exactly what they saw before.
"""

from __future__ import annotations

import logging
import os

log = logging.getLogger("crawl4ai.observability")

# Whether init_sentry() actually started the SDK. Tracked here rather than asked of the SDK at
# each call site, so report_exception() stays cheap and does not depend on which
# client-introspection helpers a given sentry-sdk version happens to expose.
_enabled = False


def init_sentry() -> bool:
    """Start Sentry if a DSN is configured. Returns whether it was enabled.

    Safe to call when `sentry-sdk` is not installed at all — the crawler must still start if
    crash reporting cannot. That matters more here than anywhere: this is a fork of an
    open-source project, and someone building the upstream image without our requirements
    should not get a container that refuses to boot.
    """
    global _enabled

    dsn = os.environ.get("SENTRY_DSN", "").strip()
    if not dsn:
        return False

    try:
        import sentry_sdk
        from sentry_sdk.integrations.logging import LoggingIntegration
    except ImportError:
        log.warning("SENTRY_DSN is set but sentry-sdk is not installed — crashes will not be reported")
        return False

    sentry_sdk.init(
        dsn=dsn,
        environment=os.environ.get("SENTRY_ENV", "production"),
        # Breadcrumbs from INFO give the lead-up to a failure — which URL, how much memory was
        # left, whether the browser had just been restarted. See the module docstring for why
        # event_level=ERROR is safe here.
        integrations=[
            LoggingIntegration(level=logging.INFO, event_level=logging.ERROR),
        ],
        traces_sample_rate=float(os.environ.get("SENTRY_TRACES_SAMPLE_RATE", "0")),
        send_default_pii=False,
        # One worker process serves many crawls. Without this, an event carries whatever tags
        # the previously-handled crawl happened to leave on the global scope.
        auto_session_tracking=False,
    )

    _enabled = True
    log.info("Sentry crash reporting enabled")
    return True


def is_enabled() -> bool:
    """Whether crash reporting is actually running. For tests and for the boot log line."""
    return _enabled


def report_exception(operation: str, url: object = None, **extra: object) -> None:
    """Report the exception currently being handled, tagged with the operation and site.

    🔴 Call this from INSIDE an `except` block — it reports the live exception, so the
    traceback is the real one rather than a string rebuilt from `str(exc)`.

    Never raises: a failure to report must not become the failure. Every caller is an error
    path that is mid-way through recording a failed task and firing a webhook, and taking that
    down to file a crash report would be strictly worse than not reporting.
    """
    if not _enabled:
        return

    try:
        import sentry_sdk

        # `new_scope` on sentry-sdk 2.x+, `push_scope` on 1.x. Resolved at call time because
        # this repo's requirements are unpinned and `push_scope` is already deprecated with
        # "will be removed in the next major version" — pinning this module to it would mean a
        # routine rebuild silently taking crash reporting down.
        scope_cm = getattr(sentry_sdk, "new_scope", None) or sentry_sdk.push_scope

        with scope_cm() as scope:
            scope.set_tag("operation", operation)
            # The host, not the full URL: one blocked site becomes one issue rather than one
            # per page. The full list goes in the context below.
            scope.set_tag("site", _host(url))
            scope.set_context("crawl", {"url": _describe(url), "operation": operation, **extra})
            sentry_sdk.capture_exception()
    except Exception:  # noqa: BLE001 - reporting a crash must not become a second crash
        log.warning("could not report an exception to Sentry", exc_info=True)


class CrawlFailure(RuntimeError):
    """A crawl that failed without anything being raised. See `report_failure`."""


def report_failure(operation: str, message: str, url: object = None, **extra: object) -> None:
    """Report a failure that arrived as a RESULT rather than an exception.

    `/crawl` decides every URL failed by inspecting `results`, so there is no live exception to
    capture — and a plain `capture_message` would group on the message text, which carries the
    site's own error string and would therefore open a new issue per site.

    Raising and immediately catching gives the event a stack trace, so Sentry groups it by
    where it happened ("every URL failed, at this line") and the varying detail stays as the
    exception value rather than becoming the identity of the issue.

    Never raises: see `report_exception`.
    """
    if not _enabled:
        return
    try:
        raise CrawlFailure(message)
    except CrawlFailure:
        report_exception(operation, url, **extra)


def _first_url(url: object) -> str:
    """The single URL to group on. Callers pass either one URL or the job's whole list."""
    if isinstance(url, (list, tuple)):
        return str(url[0]) if url else ""
    return str(url or "")


def _describe(url: object) -> str:
    """The URL, or a short summary when the caller passed a whole batch."""
    if isinstance(url, (list, tuple)) and len(url) > 1:
        return f"{_first_url(url)} (+{len(url) - 1} more)"
    return _first_url(url)


def _host(url: object) -> str:
    """The hostname to tag with, or "unknown". Never raises — see `report_exception`."""
    first = _first_url(url)
    if not first:
        return "unknown"
    try:
        from urllib.parse import urlparse

        return urlparse(first).hostname or "unknown"
    except Exception:  # noqa: BLE001
        return "unknown"
