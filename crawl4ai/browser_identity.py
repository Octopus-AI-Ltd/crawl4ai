"""Who this crawler says it is when it asks a site for a page.

⚠️ 2026-09-02, abodebed.com. Its host (WP Engine behind Cloudflare) answers **403 Forbidden**
to out-of-date browsers. Every identity hardcoded in this library was from early 2024 — the
sitemap seeder said Chrome 123, ``BrowserConfig`` said Chrome 116 — so every request was
refused. The sitemap fetch 403'd, leaving the crawl with no page list; the page fetch 403'd
too, and the caller stored the 174-character body ``403 Forbidden / nginx`` as the site's
entire content. Nothing raised an error: a refused crawl and a small website are the same
shape from here.

Proven by alternating user-agents from one machine on one IP against the same URL:

===================================  ========
Identity                             Answer
===================================  ========
Chrome 110, Chrome 123               403
Chrome 127, Chrome 140               200
``python-requests``, ``python-httpx``  403
curl, Scrapy, no user-agent at all   200
===================================  ========

Not the egress IP, not a rate limit, and the site's ``robots.txt`` welcomes crawlers.

🔴 So the version here is load-bearing, and it ages. A browser that is current today is the
thing being filtered in two years' time — which is exactly how the values above came to be
wrong. ``tests/docker/test_browser_identity.py`` holds it above a floor.
"""

#: The oldest Chrome this library is willing to claim to be. A floor, not a target.
MINIMUM_CHROME_MAJOR = 130

#: The identity used wherever the caller does not supply one of their own.
DEFAULT_BROWSER_USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/140.0.0.0 Safari/537.36"
)
