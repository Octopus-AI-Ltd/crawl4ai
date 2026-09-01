"""
A browser that has died must never be handed to the next crawl.

Regression test for 2026-08-31. Chromium is a separate process, and crawling
feelporto.com drove the container from 10% memory to 99.9% in two minutes until
the kernel took it. Nothing in the pool noticed. The dead handle stayed in
COLD_POOL, so the very next crawl — a different assistant, a different site —
was handed the corpse and failed in 40 milliseconds with:

    Browser.new_context: Target page, context or browser has been closed

That is not one site failing. It is every site failing, for everyone, until
somebody restarts the service: a plain crawl of example.com returned HTTP 500
with the same error half an hour later. The janitor could not help, because it
only closes browsers it believes are IDLE and a corpse looks exactly as idle as
a healthy browser.

Runs without crawl4ai or a browser installed: the two modules crawler_pool
imports are stubbed below, so this stays a test of the pool's bookkeeping rather
than of Playwright.

    python3 -m unittest tests.docker.test_crawler_pool_liveness -v
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
        stub.load_config = lambda: {"crawler": {"memory_threshold_percent": 95.0, "pool": {"task_recycle_percent": 60}}}
        stub.get_container_memory_percent = lambda: 10.0
        # Processes/threads, the limit that actually kills a crawl. A quiet
        # container by default, so these suites keep testing what they test.
        stub.get_container_task_percent = lambda: 5.0
        sys.modules["utils"] = stub


_install_stubs()
if DOCKER_DIR not in sys.path:
    sys.path.insert(0, DOCKER_DIR)

import crawler_pool as pool  # noqa: E402

BrowserConfig = sys.modules["crawl4ai"].BrowserConfig
AsyncWebCrawler = sys.modules["crawl4ai"].AsyncWebCrawler


class FakeBrowser:
    """Stands in for the Playwright Browser, which is what actually dies."""

    def __init__(self, connected=True, raises=False):
        self._connected = connected
        self._raises = raises

    def is_connected(self):
        if self._raises:
            raise RuntimeError("connection gone")
        return self._connected


def crawler_with_browser(browser):
    """A pooled crawler whose browser can be reached the way crawler_pool reaches it."""
    crawler = AsyncWebCrawler()
    crawler.crawler_strategy = types.SimpleNamespace(browser_manager=types.SimpleNamespace(browser=browser))
    return crawler


def live_crawler():
    return crawler_with_browser(FakeBrowser(connected=True))


def dead_crawler():
    """A crawler whose Chromium has been killed — the 2026-08-31 corpse."""
    return crawler_with_browser(FakeBrowser(connected=False))


class PoolTestCase(unittest.TestCase):
    def setUp(self):
        for store in (pool.HOT_POOL, pool.COLD_POOL, pool.LAST_USED, pool.USAGE_COUNT, pool.IN_USE, pool.LEASE_STARTED):
            store.clear()
        pool.PERMANENT = None
        pool.DEFAULT_CONFIG_SIG = None
        # A quiet container, whatever another test module left the shared `utils`
        # stub reading. These suites are about liveness and leases, not task pressure.
        pool.get_container_task_percent = lambda: 5.0

    tearDown = setUp


class IsAliveTests(PoolTestCase):
    def test_a_connected_browser_is_alive(self):
        self.assertTrue(pool.is_alive(live_crawler()))

    def test_a_disconnected_browser_is_dead(self):
        self.assertFalse(pool.is_alive(dead_crawler()))

    def test_a_closed_browser_is_dead(self):
        # BrowserManager.close() sets .browser back to None.
        self.assertFalse(pool.is_alive(crawler_with_browser(None)))

    def test_a_browser_that_raises_when_asked_is_dead(self):
        self.assertFalse(pool.is_alive(crawler_with_browser(FakeBrowser(raises=True))))

    def test_no_crawler_is_dead(self):
        self.assertFalse(pool.is_alive(None))

    def test_a_browser_we_cannot_inspect_is_left_alone(self):
        # crawl4ai's internals move. Discarding a browser merely because we could
        # not find it would relaunch Chromium on every single request.
        self.assertTrue(pool.is_alive(AsyncWebCrawler()), "an uninspectable browser must be assumed healthy")


class GetCrawlerTests(PoolTestCase):
    """What the pool actually hands out — the failure as it was seen in production."""

    def test_a_dead_cold_browser_is_not_handed_out(self):
        async def run():
            cfg = BrowserConfig("feelporto")
            sig = pool._sig(cfg)
            corpse = dead_crawler()
            pool.COLD_POOL[sig] = corpse
            pool.LAST_USED[sig] = time.time()

            handed_out = await pool.get_crawler(cfg)

            self.assertIsNot(handed_out, corpse, "the corpse must not be handed to the next crawl")
            self.assertTrue(corpse.closed, "the dead browser should be closed, not just dropped")
            self.assertIs(pool.COLD_POOL[sig], handed_out, "and the fresh one takes its place in the pool")

        asyncio.run(run())

    def test_a_dead_hot_browser_is_not_handed_out(self):
        async def run():
            cfg = BrowserConfig("busy-site")
            sig = pool._sig(cfg)
            corpse = dead_crawler()
            pool.HOT_POOL[sig] = corpse

            handed_out = await pool.get_crawler(cfg)

            self.assertIsNot(handed_out, corpse)
            self.assertNotIn(sig, pool.HOT_POOL, "a rebuilt browser starts cold again")
            self.assertIs(pool.COLD_POOL[sig], handed_out)

        asyncio.run(run())

    def test_a_dead_default_browser_is_replaced(self):
        async def run():
            cfg = BrowserConfig("default")
            pool.DEFAULT_CONFIG_SIG = pool._sig(cfg)
            pool.DEFAULT_BROWSER_CONFIG = cfg
            corpse = dead_crawler()
            pool.PERMANENT = corpse

            handed_out = await pool.get_crawler(cfg)

            self.assertIsNot(handed_out, corpse)
            self.assertIs(pool.PERMANENT, handed_out)
            self.assertTrue(corpse.closed)

        asyncio.run(run())

    def test_a_healthy_browser_is_still_reused(self):
        # The other half of the bargain: relaunching Chromium per request would
        # cost more than the bug being fixed.
        async def run():
            cfg = BrowserConfig("healthy")
            sig = pool._sig(cfg)
            pooled = live_crawler()
            pool.COLD_POOL[sig] = pooled

            self.assertIs(await pool.get_crawler(cfg), pooled)
            self.assertFalse(pooled.closed)

        asyncio.run(run())

    def test_a_rebuilt_browser_does_not_inherit_the_dead_one_s_bookkeeping(self):
        async def run():
            cfg = BrowserConfig("promoted")
            sig = pool._sig(cfg)
            pool.COLD_POOL[sig] = dead_crawler()
            pool.USAGE_COUNT[sig] = 99  # would promote it straight to hot
            pool._lease_acquire(sig)    # and a lease nobody will ever give back

            handed_out = await pool.get_crawler(cfg)

            self.assertIn(sig, pool.COLD_POOL, "a brand new browser has not earned a promotion")
            self.assertIs(pool.COLD_POOL[sig], handed_out)
            self.assertEqual(pool.USAGE_COUNT[sig], 1)
            self.assertFalse(pool.is_busy(sig), "the dead browser's lease must not pin the new one")

        asyncio.run(run())


class JanitorTests(PoolTestCase):
    """Clearing a corpse while nobody is crawling, which is when it costs least."""

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

    def test_a_dead_browser_is_swept_even_though_it_looks_freshly_used(self):
        corpse = dead_crawler()
        pool.COLD_POOL["sig"] = corpse
        pool.LAST_USED["sig"] = time.time()  # nowhere near the idle TTL

        self._run_one_janitor_pass()

        self.assertNotIn("sig", pool.COLD_POOL)
        self.assertTrue(corpse.closed)

    def test_a_dead_browser_is_swept_even_while_a_lease_says_it_is_busy(self):
        # The crawls holding that lease are already failing page by page. Keeping
        # the corpse for their sake is what spread the failure to everyone else.
        corpse = dead_crawler()
        pool.COLD_POOL["sig"] = corpse
        pool._lease_acquire("sig")

        self._run_one_janitor_pass()

        self.assertNotIn("sig", pool.COLD_POOL)
        self.assertTrue(corpse.closed)

    def test_a_healthy_busy_browser_is_left_alone(self):
        healthy = live_crawler()
        pool.COLD_POOL["sig"] = healthy
        pool._lease_acquire("sig")
        pool.LAST_USED["sig"] = time.time() - 10_000

        self._run_one_janitor_pass()

        self.assertIn("sig", pool.COLD_POOL, "the mid-crawl guard must still hold")
        self.assertFalse(healthy.closed)


if __name__ == "__main__":
    unittest.main()
