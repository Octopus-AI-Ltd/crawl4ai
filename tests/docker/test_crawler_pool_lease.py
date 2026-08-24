"""
The janitor must not close a browser that a crawl is still running on.

Regression test for 2026-08-24, when a deep crawl of week2week.co.uk had its
browser reaped after 313 seconds — just past the 300s cold-pool TTL. Idleness
was measured from when the browser was handed OUT, and nothing re-stamped it
while it worked, so a long crawl looked idle for its whole duration. The 292
pages still queued failed instantly with "'NoneType' object has no attribute
'new_context'", the job still reported `completed`, and 68 pages silently
replaced a 223-page site.

Runs without crawl4ai or a browser installed: the two imports crawler_pool needs
are stubbed below, so this stays a test of the pool's bookkeeping rather than of
Playwright.

    python3 -m unittest tests.docker.test_crawler_pool_lease -v
"""
import asyncio
import os
import sys
import time
import types
import unittest
from unittest import mock

DOCKER_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))), "deploy", "docker")


def _install_stubs():
    """Stub the two modules crawler_pool imports, so it can be loaded on its own."""
    if "crawl4ai" not in sys.modules:
        stub = types.ModuleType("crawl4ai")

        class BrowserConfig:
            def __init__(self, name="default"):
                self.name = name

            def to_dict(self):
                return {"name": self.name}

        class AsyncWebCrawler:
            def __init__(self, config=None, thread_safe=False):
                self.config = config
                self.closed = False

            async def start(self):
                return self

            async def close(self):
                self.closed = True

            async def arun(self, *args, **kwargs):
                return "crawled"

            async def arun_many(self, *args, **kwargs):
                return ["crawled"]

        stub.BrowserConfig = BrowserConfig
        stub.AsyncWebCrawler = AsyncWebCrawler
        sys.modules["crawl4ai"] = stub

    if "utils" not in sys.modules:
        stub = types.ModuleType("utils")
        stub.load_config = lambda: {"crawler": {"memory_threshold_percent": 95.0}}
        stub.get_container_memory_percent = lambda: 10.0
        sys.modules["utils"] = stub


_install_stubs()
if DOCKER_DIR not in sys.path:
    sys.path.insert(0, DOCKER_DIR)

import crawler_pool as pool  # noqa: E402


class PoolTestCase(unittest.TestCase):
    def setUp(self):
        for store in (pool.HOT_POOL, pool.COLD_POOL, pool.LAST_USED, pool.USAGE_COUNT, pool.IN_USE, pool.LEASE_STARTED):
            store.clear()
        pool.PERMANENT = None
        pool.DEFAULT_CONFIG_SIG = None

    tearDown = setUp


class IsBusyTests(PoolTestCase):
    def test_idle_browser_is_not_busy(self):
        self.assertFalse(pool.is_busy("sig"))

    def test_browser_with_a_crawl_running_is_busy(self):
        pool._lease_acquire("sig")
        self.assertTrue(pool.is_busy("sig"))

    def test_stays_busy_until_the_last_crawl_finishes(self):
        pool._lease_acquire("sig")
        pool._lease_acquire("sig")
        pool._lease_release("sig")
        self.assertTrue(pool.is_busy("sig"), "one crawl finishing must not free a browser another is using")
        pool._lease_release("sig")
        self.assertFalse(pool.is_busy("sig"))

    def test_idle_clock_restarts_when_the_work_stops(self):
        pool._lease_acquire("sig")
        pool.LAST_USED["sig"] = time.time() - 10_000  # as if handed out long ago
        pool._lease_release("sig")
        self.assertLess(time.time() - pool.LAST_USED["sig"], 1, "TTL must count from when the crawl ended")

    def test_a_lease_that_is_never_returned_is_eventually_disbelieved(self):
        pool._lease_acquire("sig")
        pool.LEASE_STARTED["sig"] = time.time() - (pool.MAX_LEASE_SEC + 1)
        self.assertFalse(pool.is_busy("sig"), "a leaked lease must not pin a browser forever")


class LeaseThroughCrawlerTests(PoolTestCase):
    def test_a_running_arun_holds_the_lease_for_its_whole_duration(self):
        async def run():
            cfg = sys.modules["crawl4ai"].BrowserConfig("deep-crawl")
            sig = pool._sig(cfg)

            finish = asyncio.Event()

            class SlowCrawler(sys.modules["crawl4ai"].AsyncWebCrawler):
                async def arun(self, *args, **kwargs):
                    await finish.wait()
                    return "crawled"

            crawler = pool._track_usage(SlowCrawler(), sig)
            pool.COLD_POOL[sig] = crawler
            pool.LAST_USED[sig] = time.time()

            task = asyncio.create_task(crawler.arun("https://example.com"))
            await asyncio.sleep(0)  # let it start

            self.assertTrue(pool.is_busy(sig), "the browser must count as busy while the crawl runs")

            # As far as the janitor's timestamp is concerned this browser has been
            # idle for an hour — which is exactly the trap. It must survive anyway.
            pool.LAST_USED[sig] = time.time() - 3600
            self.assertTrue(pool.is_busy(sig))

            finish.set()
            self.assertEqual(await task, "crawled")
            self.assertFalse(pool.is_busy(sig), "the lease must be given back when the crawl ends")

        asyncio.run(run())

    def test_a_failed_crawl_still_gives_the_lease_back(self):
        async def run():
            sig = "sig"

            class FailingCrawler(sys.modules["crawl4ai"].AsyncWebCrawler):
                async def arun(self, *args, **kwargs):
                    raise RuntimeError("boom")

            crawler = pool._track_usage(FailingCrawler(), sig)
            with self.assertRaises(RuntimeError):
                await crawler.arun("https://example.com")
            self.assertFalse(pool.is_busy(sig))

        asyncio.run(run())

    def test_a_stream_holds_the_lease_until_it_is_consumed(self):
        async def run():
            sig = "sig"

            class StreamingCrawler(sys.modules["crawl4ai"].AsyncWebCrawler):
                async def arun_many(self, *args, **kwargs):
                    async def gen():
                        for page in ("a", "b"):
                            yield page

                    return gen()

            crawler = pool._track_usage(StreamingCrawler(), sig)
            stream = await crawler.arun_many(urls=["https://example.com"])
            self.assertTrue(pool.is_busy(sig), "work happens as the stream is read, so the lease must outlive the call")

            pages = []
            async for page in stream:
                pages.append(page)
                self.assertTrue(pool.is_busy(sig))

            self.assertEqual(pages, ["a", "b"])
            self.assertFalse(pool.is_busy(sig))

        asyncio.run(run())

    def test_a_non_streaming_arun_many_releases_immediately(self):
        async def run():
            sig = "sig"
            crawler = pool._track_usage(sys.modules["crawl4ai"].AsyncWebCrawler(), sig)
            self.assertEqual(await crawler.arun_many(urls=["https://example.com"]), ["crawled"])
            self.assertFalse(pool.is_busy(sig))

        asyncio.run(run())

    def test_wrapping_is_applied_once_per_crawler(self):
        sig = "sig"
        crawler = pool._track_usage(sys.modules["crawl4ai"].AsyncWebCrawler(), sig)
        once = crawler.arun
        self.assertIs(pool._track_usage(crawler, sig).arun, once, "re-wrapping would stack a lease per acquisition")


class JanitorTests(PoolTestCase):
    """The guard as the janitor actually applies it."""

    def _run_one_janitor_pass(self):
        real_sleep = asyncio.sleep
        passes = {"n": 0}

        async def fake_sleep(_seconds):
            passes["n"] += 1
            await real_sleep(0)
            if passes["n"] > 1:
                raise asyncio.CancelledError

        async def run():
            with mock.patch.object(asyncio, "sleep", fake_sleep):
                with self.assertRaises(asyncio.CancelledError):
                    await pool.janitor()

        asyncio.run(run())

    def test_an_idle_cold_browser_is_closed(self):
        crawler = sys.modules["crawl4ai"].AsyncWebCrawler()
        pool.COLD_POOL["sig"] = crawler
        pool.LAST_USED["sig"] = time.time() - 10_000
        self._run_one_janitor_pass()
        self.assertTrue(crawler.closed)
        self.assertNotIn("sig", pool.COLD_POOL)

    def test_a_busy_cold_browser_is_left_alone(self):
        crawler = sys.modules["crawl4ai"].AsyncWebCrawler()
        pool.COLD_POOL["sig"] = crawler
        pool.LAST_USED["sig"] = time.time() - 10_000  # looks idle by timestamp
        pool._lease_acquire("sig")  # but a crawl is running on it
        pool.LAST_USED["sig"] = time.time() - 10_000
        self._run_one_janitor_pass()
        self.assertFalse(crawler.closed, "closing this browser is what killed 292 queued pages")
        self.assertIn("sig", pool.COLD_POOL)

    def test_a_busy_hot_browser_is_left_alone(self):
        crawler = sys.modules["crawl4ai"].AsyncWebCrawler()
        pool.HOT_POOL["sig"] = crawler
        pool._lease_acquire("sig")
        pool.LAST_USED["sig"] = time.time() - 10_000
        self._run_one_janitor_pass()
        self.assertFalse(crawler.closed)

    def test_a_busy_default_browser_is_left_alone(self):
        crawler = sys.modules["crawl4ai"].AsyncWebCrawler()
        pool.PERMANENT = crawler
        pool.DEFAULT_CONFIG_SIG = "sig"
        pool._lease_acquire("sig")
        pool.LAST_USED["sig"] = time.time() - 10_000
        self._run_one_janitor_pass()
        self.assertFalse(crawler.closed)
        self.assertIsNotNone(pool.PERMANENT)


if __name__ == "__main__":
    unittest.main()
