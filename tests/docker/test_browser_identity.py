"""Who this crawler says it is.

Written 2026-09-02, after abodebed.com's assistant was found holding a single
174-character page reading ``403 Forbidden / nginx`` as its entire knowledge, with
the customer-facing screen showing a green "Done" beside it.

The site was never blocking us on purpose — its ``robots.txt`` welcomes crawlers.
Its host refuses out-of-date browsers, and every identity hardcoded in this library
was from early 2024. Proven by alternating user-agents from one machine on one IP
against the same URL: Chrome 110 and 123 refused, Chrome 127 and 140 served,
``python-httpx`` refused, curl served.

What makes it worth a test rather than a one-line fix: nothing failed. A sitemap
that 403s returns an empty list, and an empty list is indistinguishable from a site
with no sitemap. A page that 403s returns a body, and a body is indistinguishable
from content. The version here ages every month whether anyone looks at it or not,
and it will silently re-enter the blocked range if nothing holds it.
"""

import importlib.util
import re
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "deploy" / "docker"))


def _load(name: str, relative: str):
    """Import one module by path, without dragging in the whole package's dependencies."""
    spec = importlib.util.spec_from_file_location(name, ROOT / relative)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


browser_identity = _load("browser_identity", "crawl4ai/browser_identity.py")

DEFAULT_BROWSER_USER_AGENT = browser_identity.DEFAULT_BROWSER_USER_AGENT
MINIMUM_CHROME_MAJOR = browser_identity.MINIMUM_CHROME_MAJOR


class DefaultUserAgentTests(unittest.TestCase):
    def test_claims_a_chrome_recent_enough_not_to_be_filtered(self):
        match = re.search(r"Chrome/(\d+)", DEFAULT_BROWSER_USER_AGENT)
        self.assertIsNotNone(match, "the user-agent should name a Chrome version")
        major = int(match.group(1))
        self.assertGreaterEqual(
            major, MINIMUM_CHROME_MAJOR,
            f"Chrome {major} is below the floor of {MINIMUM_CHROME_MAJOR}; hosts that "
            "filter stale browsers refuse it. Bump DEFAULT_BROWSER_USER_AGENT.",
        )

    def test_the_floor_itself_cannot_be_quietly_lowered(self):
        # Otherwise the cheapest way to make the test above pass is to move the floor, which
        # is precisely the change nobody would notice. Raising it is fine and expected;
        # lowering it has to be a deliberate edit here, visible in a diff.
        self.assertGreaterEqual(MINIMUM_CHROME_MAJOR, 130)

    def test_does_not_announce_itself_as_a_script(self):
        # The seeder's requests go out over httpx, and `python-httpx/...` is refused by
        # the same host that refused Chrome 123.
        self.assertNotRegex(DEFAULT_BROWSER_USER_AGENT, r"(?i)python|httpx|requests|scrapy|bot")

    def test_is_a_well_formed_user_agent(self):
        # The old seeder string carried a stray "+" before AppleWebKit. Harmless in
        # itself, but it is the kind of thing a fingerprinting rule notices.
        self.assertNotIn("+AppleWebKit", DEFAULT_BROWSER_USER_AGENT)
        self.assertTrue(DEFAULT_BROWSER_USER_AGENT.startswith("Mozilla/5.0 ("))
        self.assertTrue(DEFAULT_BROWSER_USER_AGENT.endswith("Safari/537.36"))


class NoStaleIdentitiesLeftBehindTests(unittest.TestCase):
    """The whole point is that there is ONE identity, not four that drift apart."""

    FILES = [
        "crawl4ai/async_url_seeder.py",
        "crawl4ai/async_configs.py",
    ]

    def test_no_module_hardcodes_a_browser_that_would_be_refused(self):
        for relative in self.FILES:
            text = (ROOT / relative).read_text()
            for major in re.findall(r"Chrome/(\d+)", text):
                self.assertGreaterEqual(
                    int(major), MINIMUM_CHROME_MAJOR,
                    f"{relative} still hardcodes Chrome {major}. Every hardcoded identity "
                    "in this library was stale at once, which is why nothing caught it — "
                    "use DEFAULT_BROWSER_USER_AGENT.",
                )


class SeederAcceptsAnIdentityTests(unittest.TestCase):
    """api.py calls AsyncUrlSeeder(user_agent=...); the parameter has to exist.

    Checked against the source rather than by importing, because importing the seeder
    pulls in the whole crawler stack. If this contract is dropped, /seed raises on every
    request — and a seed that raises is reported to the caller as "no sitemap", which is
    the same silent shape as the bug this all started with.
    """

    def test_the_seeder_takes_a_user_agent_and_uses_it(self):
        import ast

        source = (ROOT / "crawl4ai/async_url_seeder.py").read_text()
        seeder = next(
            node for node in ast.walk(ast.parse(source))
            if isinstance(node, ast.ClassDef) and node.name == "AsyncUrlSeeder"
        )
        init = next(
            node for node in seeder.body
            if isinstance(node, ast.FunctionDef) and node.name == "__init__"
        )
        params = [arg.arg for arg in init.args.args + init.args.kwonlyargs]
        self.assertIn("user_agent", params)
        self.assertIn('"User-Agent": self.user_agent', source)


class SeedRequestTests(unittest.TestCase):
    """The caller must be able to say who it is on /seed."""

    def test_the_endpoint_accepts_a_user_agent(self):
        try:
            from schemas import SeedRequest  # noqa: E402
        except ModuleNotFoundError as missing:  # pragma: no cover - env without pydantic
            self.skipTest(f"schemas needs {missing.name}")

        request = SeedRequest(urls=["https://www.abodebed.com"], user_agent="Test/1.0")
        self.assertEqual(request.user_agent, "Test/1.0")

    def test_it_is_optional_and_absent_means_use_the_default(self):
        try:
            from schemas import SeedRequest  # noqa: E402
        except ModuleNotFoundError as missing:  # pragma: no cover
            self.skipTest(f"schemas needs {missing.name}")

        # None, not "" — the seeder treats a falsy value as "no preference" and falls back
        # to DEFAULT_BROWSER_USER_AGENT. An empty string sent as a real header would be a
        # third identity, and an unrecognised one at that.
        self.assertIsNone(SeedRequest(urls=["https://www.abodebed.com"]).user_agent)


if __name__ == "__main__":
    unittest.main()
