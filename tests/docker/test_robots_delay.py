"""How long a site asked us to wait, and whether we listened.

Written 2026-09-02, after abodebed.com, portlandbrown.com, princesstreetsuites.com
and oldwaverley.co.uk all began serving Cloudflare bot challenges to this crawler's
egress IP. Three of the four publish ``Crawl-delay: 10``; we waited 1-2 seconds and
had never read the directive.

The case that matters most is the malformed one. abodebed.com opens its robots.txt
with a bare ``Crawl-delay: 10`` above every ``User-agent`` line, so a by-the-book
parser — Python's own ``urllib.robotparser`` included — reads the site as asking for
nothing at all.
"""

import importlib.util
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


robots_delay = _load("robots_delay", "crawl4ai/robots_delay.py")

parse_crawl_delay = robots_delay.parse_crawl_delay
choose_base_delay = robots_delay.choose_base_delay
robots_url_for = robots_delay.robots_url_for
MAX_CRAWL_DELAY_SECONDS = robots_delay.MAX_CRAWL_DELAY_SECONDS
MAX_POLITE_CRAWL_SECONDS = robots_delay.MAX_POLITE_CRAWL_SECONDS

# Reproduced from https://www.abodebed.com/robots.txt on 2026-09-02.
ABODEBED_ROBOTS = """Crawl-delay: 10
User-agent: *
Disallow: /?wc-ajax=get_refreshed_fragments

# START YOAST BLOCK
# ---------------------------
User-agent: *
Disallow:

Sitemap: https://www.abodebed.com/sitemap_index.xml
# ---------------------------
# END YOAST BLOCK"""


class ReadingWhatTheSiteAsksFor(unittest.TestCase):
    def test_a_delay_written_above_every_group_still_counts(self):
        """The real file that prompted this. A strict parser reads it as no delay."""
        self.assertEqual(parse_crawl_delay(ABODEBED_ROBOTS), 10.0)

    def test_the_ordinary_case(self):
        self.assertEqual(parse_crawl_delay("User-agent: *\nCrawl-delay: 10"), 10.0)

    def test_a_site_asking_for_nothing_returns_nothing(self):
        """Not zero. Zero is a delay; None is the absence of one, and they differ."""
        self.assertIsNone(parse_crawl_delay("User-agent: *\nDisallow: /private"))

    def test_a_group_named_for_us_beats_the_wildcard(self):
        robots = "User-agent: *\nCrawl-delay: 10\n\nUser-agent: octopus\nCrawl-delay: 2"
        self.assertEqual(parse_crawl_delay(robots, "octopus"), 2.0)
        self.assertEqual(parse_crawl_delay(robots, "somebody-else"), 10.0)

    def test_consecutive_user_agent_lines_share_one_group(self):
        robots = "User-agent: alpha\nUser-agent: beta\nCrawl-delay: 7"
        self.assertEqual(parse_crawl_delay(robots, "beta"), 7.0)
        self.assertIsNone(parse_crawl_delay(robots, "gamma"))

    def test_a_delay_belongs_only_to_its_own_group(self):
        robots = "User-agent: alpha\nCrawl-delay: 7\n\nUser-agent: *\nDisallow:"
        self.assertIsNone(parse_crawl_delay(robots, "gamma"))

    def test_comments_and_junk_are_ignored_not_guessed_at(self):
        self.assertIsNone(parse_crawl_delay("User-agent: *\nCrawl-delay: soon # nonsense"))
        self.assertIsNone(parse_crawl_delay("User-agent: *\nCrawl-delay: -5"))
        self.assertEqual(parse_crawl_delay("User-agent: *\nCrawl-delay: 10 # be nice"), 10.0)

    def test_case_and_spacing_do_not_matter(self):
        self.assertEqual(parse_crawl_delay("USER-AGENT: *\n  crawl-delay :  4  "), 4.0)

    def test_an_empty_file_is_not_an_error(self):
        self.assertIsNone(parse_crawl_delay(""))


class ChoosingHowLongToActuallyWait(unittest.TestCase):
    DEFAULT = (1.0, 2.0)

    def test_a_site_that_asks_for_nothing_is_crawled_exactly_as_before(self):
        self.assertEqual(choose_base_delay(None, self.DEFAULT), self.DEFAULT)

    def test_a_published_delay_is_honoured(self):
        low, high = choose_base_delay(10.0, self.DEFAULT, page_count=51)
        self.assertEqual(low, 10.0)
        self.assertGreater(high, low, "the jitter must survive, or we arrive metronomically")

    def test_this_can_only_ever_slow_us_down(self):
        """🔴 A site asking for less than our floor is not licence to speed up."""
        self.assertEqual(choose_base_delay(0.2, self.DEFAULT), self.DEFAULT)

    def test_no_single_wait_may_exceed_the_cap(self):
        low, _ = choose_base_delay(3_600.0, self.DEFAULT, page_count=2)
        self.assertEqual(low, MAX_CRAWL_DELAY_SECONDS)

    def test_a_big_site_does_not_hold_a_browser_open_for_hours(self):
        """5,000 pages at the published 10s would be 13.9 hours, so it is not used.

        The budget trims the PUBLISHED delay; it cannot push us below our own floor,
        so on a site this size the answer is the default rather than the budget. What
        matters is that the ten seconds is not what comes back.
        """
        pages = 5_000
        low, _ = choose_base_delay(10.0, self.DEFAULT, page_count=pages)
        affordable = MAX_POLITE_CRAWL_SECONDS / pages
        self.assertEqual(low, max(self.DEFAULT[0], affordable))
        self.assertLess(low, 10.0)

    def test_the_budget_trims_a_published_delay_that_is_merely_long(self):
        """300 pages at 10s is 50 minutes; trimmed to fit the half-hour budget."""
        pages = 300
        low, _ = choose_base_delay(10.0, self.DEFAULT, page_count=pages)
        self.assertAlmostEqual(low, MAX_POLITE_CRAWL_SECONDS / pages)
        self.assertLessEqual(low * pages, MAX_POLITE_CRAWL_SECONDS)

    def test_trimming_for_a_big_site_never_goes_below_the_default(self):
        low, high = choose_base_delay(10.0, self.DEFAULT, page_count=10_000_000)
        self.assertEqual((low, high), self.DEFAULT)

    def test_an_unknown_page_count_does_not_divide_by_zero(self):
        for count in (None, 0):
            low, _ = choose_base_delay(10.0, self.DEFAULT, page_count=count)
            self.assertEqual(low, 10.0)

    def test_the_caps_cannot_be_quietly_raised(self):
        """The point of the numbers is that they are small. Guarded like the version floor."""
        self.assertLessEqual(MAX_CRAWL_DELAY_SECONDS, 30.0)
        self.assertLessEqual(MAX_POLITE_CRAWL_SECONDS, 3_600.0)


class FindingTheRightRobotsFile(unittest.TestCase):
    def test_it_is_per_host(self):
        self.assertEqual(robots_url_for("https://www.abodebed.com/apartment/apartment-1"),
                         "https://www.abodebed.com/robots.txt")

    def test_a_bare_host_is_assumed_https(self):
        self.assertEqual(robots_url_for("abodebed.com"), "https://abodebed.com/robots.txt")

    def test_the_scheme_is_kept(self):
        self.assertEqual(robots_url_for("http://example.com/x"), "http://example.com/robots.txt")

    def test_something_with_no_host_asks_nobody(self):
        self.assertIsNone(robots_url_for("raw://<html></html>"))


if __name__ == "__main__":
    unittest.main()
