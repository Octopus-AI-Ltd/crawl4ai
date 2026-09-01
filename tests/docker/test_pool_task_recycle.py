"""
An idle browser hoarding the container's processes must be thrown away.

Regression test for 2026-09-01. Chromium keeps its renderer processes after the
pages are closed and only returns them when the browser exits. Measured on Railway
with NO crawl running, straight after a 120-page crawl:

    pids.current = 798 of 1000     28 chrome processes, 725 threads
    memory       = 1.7 GB of 6.0 GB

The next crawl therefore began with about 200 tasks of headroom and needed roughly
740. It hit the ceiling mid-run, `clone()` began failing, the Playwright driver's
pipe closed, and every page still queued died with "Connection closed while reading
from the driver". A 500-page crawl of feelporto.com lost 435 pages that way.

Nothing in the pool was watching this. The memory brake could not help: memory was
at 28% throughout, and raising the container from 3.7 GB to 6 GB changed nothing.

⚠️ The second half of this suite matters as much as the first: a BUSY browser must
never be recycled. Closing one mid-crawl is the same failure, self-inflicted - it
is what [[the janitor did on 2026-08-24]] and it cost 292 pages.

    python3 -m unittest tests.docker.test_pool_task_recycle -v
"""
import asyncio
import os
import sys
import types
import unittest

DOCKER_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))), "deploy", "docker")

# Task usage the stubbed `utils` reports; tests set it per-case.
TASK_PERCENT = {"value": 5.0}


def _install_stubs():
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
        stub.get_container_task_percent = lambda: TASK_PERCENT["value"]
        sys.modules["utils"] = stub


_install_stubs()
if DOCKER_DIR not in sys.path:
    sys.path.insert(0, DOCKER_DIR)

import crawler_pool as pool  # noqa: E402

BrowserConfig = sys.modules["crawl4ai"].BrowserConfig
AsyncWebCrawler = sys.modules["crawl4ai"].AsyncWebCrawler

# The pool caches the reader at import time in some paths, so point both at ours.
pool.get_container_task_percent = lambda: TASK_PERCENT["value"]


class FakeBrowser:
    def is_connected(self):
        return True


def live_crawler():
    crawler = AsyncWebCrawler()
    crawler.crawler_strategy = types.SimpleNamespace(browser_manager=types.SimpleNamespace(browser=FakeBrowser()))
    return crawler


def run(coro):
    return asyncio.get_event_loop().run_until_complete(coro)


class RecycleTestCase(unittest.TestCase):
    def setUp(self):
        for store in (pool.HOT_POOL, pool.COLD_POOL, pool.LAST_USED, pool.USAGE_COUNT, pool.IN_USE, pool.LEASE_STARTED):
            store.clear()
        pool.PERMANENT = None
        pool.DEFAULT_CONFIG_SIG = None
        pool.TASK_RECYCLE_LIMIT = 60.0
        TASK_PERCENT["value"] = 5.0
        # Re-bound every test, not once at import: the other pool suites pin this
        # to a constant in their own setUp, and whichever module ran last wins.
        pool.get_container_task_percent = lambda: TASK_PERCENT["value"]
        asyncio.set_event_loop(asyncio.new_event_loop())

    def tearDown(self):
        # `utils` is stubbed once per process by whichever suite imports first, and
        # this one rebinds pool.get_container_task_percent globally. Leaving it
        # reading 99% made a later suite watch the pool recycle a perfectly healthy
        # browser and fail for reasons that had nothing to do with it.
        TASK_PERCENT["value"] = 5.0


class ColdAndHotPoolTests(RecycleTestCase):
    def test_a_quiet_container_reuses_the_browser(self):
        cfg = BrowserConfig("site")
        sig = pool._sig(cfg)
        original = live_crawler()
        pool.COLD_POOL[sig] = original

        TASK_PERCENT["value"] = 5.0
        got = run(pool.get_crawler(cfg))

        self.assertIs(got, original, "a browser must not be thrown away for no reason")
        self.assertFalse(original.closed)

    def test_the_observed_798_of_1000_recycles_the_browser(self):
        cfg = BrowserConfig("site")
        sig = pool._sig(cfg)
        original = live_crawler()
        pool.COLD_POOL[sig] = original

        TASK_PERCENT["value"] = 79.8
        got = run(pool.get_crawler(cfg))

        self.assertIsNot(got, original, "the hoarding browser must not be handed out again")
        self.assertTrue(original.closed, "closing it is the only thing that reclaims the processes")

    def test_a_recycled_browser_leaves_no_bookkeeping_behind(self):
        cfg = BrowserConfig("site")
        sig = pool._sig(cfg)
        pool.COLD_POOL[sig] = live_crawler()
        pool.USAGE_COUNT[sig] = 7

        TASK_PERCENT["value"] = 79.8
        run(pool.get_crawler(cfg))

        # A stale usage count would promote the fresh browser straight to hot.
        self.assertEqual(pool.USAGE_COUNT.get(sig), 1)

    def test_the_hot_pool_is_recycled_too(self):
        cfg = BrowserConfig("site")
        sig = pool._sig(cfg)
        original = live_crawler()
        pool.HOT_POOL[sig] = original

        TASK_PERCENT["value"] = 79.8
        got = run(pool.get_crawler(cfg))

        self.assertIsNot(got, original)
        self.assertTrue(original.closed)

    def test_the_default_browser_is_recycled_too(self):
        cfg = BrowserConfig("default")
        run(pool.init_permanent(cfg))
        first = run(pool.get_crawler(cfg))

        TASK_PERCENT["value"] = 79.8
        second = run(pool.get_crawler(cfg))

        self.assertIsNot(second, first)
        self.assertTrue(first.closed)


class NeverRecycleABusyBrowserTests(RecycleTestCase):
    """
    The failure mode this must not reintroduce.

    Closing a browser with a crawl running on it kills every page still queued
    behind it. That is the 2026-08-24 janitor bug, and recycling for task pressure
    would be the same wound from a different direction.
    """

    def test_a_busy_browser_is_left_alone_however_high_the_task_count(self):
        cfg = BrowserConfig("site")
        sig = pool._sig(cfg)
        original = live_crawler()
        pool.COLD_POOL[sig] = original
        pool._lease_acquire(sig)  # a crawl is running on it

        TASK_PERCENT["value"] = 99.0
        recycled = run(pool._recycle_if_starving_container(sig, pool.COLD_POOL, "cold pool"))

        self.assertFalse(recycled)
        self.assertFalse(original.closed, "a running crawl must be allowed to finish")
        self.assertIs(pool.COLD_POOL.get(sig), original)

    def test_it_is_recycled_once_the_crawl_finishes(self):
        cfg = BrowserConfig("site")
        sig = pool._sig(cfg)
        original = live_crawler()
        pool.COLD_POOL[sig] = original
        pool._lease_acquire(sig)
        pool._lease_release(sig)

        TASK_PERCENT["value"] = 99.0
        recycled = run(pool._recycle_if_starving_container(sig, pool.COLD_POOL, "cold pool"))

        self.assertTrue(recycled)
        self.assertTrue(original.closed)

    def test_an_uncapped_container_never_recycles(self):
        # No cgroup pids limit reads as 0.0, not as "full".
        cfg = BrowserConfig("site")
        sig = pool._sig(cfg)
        original = live_crawler()
        pool.COLD_POOL[sig] = original

        TASK_PERCENT["value"] = 0.0
        recycled = run(pool._recycle_if_starving_container(sig, pool.COLD_POOL, "cold pool"))

        self.assertFalse(recycled)
        self.assertFalse(original.closed)


if __name__ == "__main__":
    unittest.main(verbosity=2)
