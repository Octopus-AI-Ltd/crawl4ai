"""How long a site has asked us to wait between requests, and whether we may.

Written 2026-09-02, after four customer sites started serving Cloudflare bot
challenges to this crawler's egress IP within hours of being crawled. Three of the
four publish ``Crawl-delay: 10`` in ``robots.txt``. We were waiting 1-2 seconds and
had never read the directive at all — ``crawl_delay`` appeared nowhere in the
codebase.

Being refused is worse than being slow. A blocked site cannot be re-read at any
speed, its stored knowledge goes stale with no way to refresh it, and getting
unblocked means asking the customer to allowlist us — which is a conversation about
our crawler's manners that we would rather not have.

Two clamps, because a delay taken literally is its own outage:

* no single wait longer than :data:`MAX_CRAWL_DELAY_SECONDS`, so one robots.txt
  asking for an hour between pages cannot pin a browser open indefinitely;
* and, when the number of pages is known up front, the delay is reduced to keep the
  whole crawl inside :data:`MAX_POLITE_CRAWL_SECONDS` — never below the configured
  default, so this can only ever make us more polite than we were.
"""

from __future__ import annotations

from typing import Iterable, Optional, Sequence, Tuple
from urllib.parse import urlparse

__all__ = [
    "MAX_CRAWL_DELAY_SECONDS",
    "MAX_POLITE_CRAWL_SECONDS",
    "parse_crawl_delay",
    "choose_base_delay",
    "robots_url_for",
]

#: The longest we will wait between two pages, however long the site asks for.
MAX_CRAWL_DELAY_SECONDS = 10.0

#: The longest a whole crawl may spend waiting before the per-page delay is trimmed
#: to fit. Thirty minutes: long enough that a small site is crawled exactly as asked,
#: short enough that a thousand-page site does not hold a browser open for hours.
MAX_POLITE_CRAWL_SECONDS = 1_800.0


def robots_url_for(url: str) -> Optional[str]:
    """The robots.txt that governs ``url``, or None if there is nobody to ask.

    ⚠️ Only http and https have one. crawl4ai also accepts ``raw:`` URLs carrying the
    HTML inline, and those parse into a plausible-looking host — asking it for a
    robots.txt produces a request to a hostname made of markup.
    """
    parsed = urlparse(url if "://" in url else f"https://{url}")
    scheme = parsed.scheme or "https"
    if scheme not in ("http", "https"):
        return None
    if not parsed.netloc:
        return None
    return f"{scheme}://{parsed.netloc}/robots.txt"


def _matching_tokens(user_agent: Optional[str]) -> Sequence[str]:
    """Group names that apply to us, most specific first."""
    if not user_agent:
        return ("*",)
    return (user_agent.strip().lower(), "*")


def parse_crawl_delay(robots_txt: str, user_agent: Optional[str] = None) -> Optional[float]:
    """The ``Crawl-delay`` that applies to us, in seconds, or None if unpublished.

    ⚠️ Deliberately tolerant of a malformed file, because the file that prompted this
    is malformed. abodebed.com opens with a bare ``Crawl-delay: 10`` BEFORE any
    ``User-agent`` line, which by the letter of the standard belongs to no group at
    all — Python's own ``urllib.robotparser`` drops it. A directive written above the
    groups is plainly meant for everyone, so it is read as the ``*`` group's.

    A group named for us wins over ``*``. Consecutive ``User-agent`` lines share one
    group, as the standard requires. An unparseable or negative value is ignored
    rather than guessed at.
    """
    delays: dict[str, float] = {}
    # Directives seen before the first `User-agent` line. See the docstring.
    current: list[str] = ["*"]
    starting_group = False

    for raw_line in robots_txt.splitlines():
        line = raw_line.split("#", 1)[0].strip()
        if not line or ":" not in line:
            continue
        field, _, value = line.partition(":")
        field = field.strip().lower()
        value = value.strip()

        if field == "user-agent":
            if not starting_group:
                current = []
                starting_group = True
            current.append(value.lower())
            continue

        starting_group = False
        if field != "crawl-delay" or not current:
            continue
        try:
            seconds = float(value)
        except ValueError:
            continue
        if seconds <= 0:
            continue
        for token in current:
            # First value wins for a group, matching how the major crawlers read a
            # repeated directive.
            delays.setdefault(token, seconds)

    for token in _matching_tokens(user_agent):
        if token in delays:
            return delays[token]
    return None


def choose_base_delay(
    published: Optional[float],
    default_range: Iterable[float],
    page_count: Optional[int] = None,
) -> Tuple[float, float]:
    """The ``(low, high)`` seconds to wait between pages of one site.

    Returns ``default_range`` untouched when the site publishes nothing, so a site
    that has not asked for anything is crawled exactly as before. Otherwise the
    published delay is honoured, clamped by both limits, and never allowed to drop
    below the default — this makes us slower, never faster.
    """
    low, high = (float(value) for value in default_range)
    if published is None:
        return (low, high)

    delay = min(float(published), MAX_CRAWL_DELAY_SECONDS)

    # Known page count: trim the wait so the whole crawl still finishes. `page_count`
    # of 1 cannot exceed the budget, and 0/None means "we do not know" rather than
    # "no pages", so neither divides.
    if page_count and page_count > 0:
        affordable = MAX_POLITE_CRAWL_SECONDS / page_count
        delay = min(delay, affordable)

    # Politeness only. A published delay under our own floor is not licence to speed up.
    if delay <= low:
        return (low, high)

    # Keep the original jitter, which is what stops a crawl arriving in a metronomic
    # pattern that itself looks automated.
    spread = max(high - low, 0.0)
    return (delay, delay + spread)
