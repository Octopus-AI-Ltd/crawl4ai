"""
The memory brake must watch the container, not the machine it happens to sit on.

Regression test for 2026-08-31. MemoryAdaptiveDispatcher throttles on
`get_true_memory_usage_percent()`, which asked psutil — and psutil reports the
HOST. On Railway that is a 322.7 GB machine while the container is held to 3.7 GB,
so the two readings were, live:

    dispatcher reads (host) : 47.9%
    pool reads (container)  : 12.4%

For the dispatcher's 90% brake to engage, the shared host would have had to pass
290 GB. Our container dies at 3.7 GB — about 1.1% of what it was watching. The
brake could not fire, so a crawl of feelporto.com opened 20 pages at once and went
from 10% to 99.9% of the container in two minutes until Chromium was killed. The
dead browser it left behind then took the whole platform's crawling down.

Runs without crawl4ai installed: resource_limits.py is loaded straight from its
path with psutil stubbed, so none of the library's dependencies are needed.

    python3 -m unittest tests.docker.test_resource_limits -v
"""
import os
import sys
import types
import unittest
from unittest import mock

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

HOST_TOTAL_BYTES = 322_704_000_000  # what psutil reports on the Railway host
CONTAINER_LIMIT_BYTES = 3_700_000_000  # what the container is actually held to


def _install_psutil_stub():
    if "psutil" not in sys.modules:
        stub = types.ModuleType("psutil")
        stub.virtual_memory = lambda: types.SimpleNamespace(total=HOST_TOTAL_BYTES, percent=47.9)
        sys.modules["psutil"] = stub


_install_psutil_stub()


def _load_resource_limits():
    """
    Load the module from its source, so `crawl4ai/__init__` is not dragged in with it.

    Compiled here rather than through importlib because a bytecode cache will
    happily serve a stale copy. macOS's system Python keeps that cache OUTSIDE the
    repo, under ~/Library/Caches/com.apple.python, where clearing __pycache__ does
    not touch it — and it is validated on the source's size and mtime, so editing
    `8` to `4` in the same second passes both checks. That cost half an hour of
    reading a correct file while the tests ran the old one.
    """
    path = os.path.join(REPO_ROOT, "crawl4ai", "resource_limits.py")
    with open(path, encoding="utf-8") as handle:
        source = handle.read()
    module = types.ModuleType("resource_limits_under_test")
    module.__file__ = path
    exec(compile(source, path, "exec"), module.__dict__)
    return module


cm = _load_resource_limits()


class FakeCgroupFile:
    """Stands in for one of the cgroup files, including the ways it can be absent."""

    def __init__(self, contents=None, error=None):
        self.contents = contents
        self.error = error

    def read_text(self):
        if self.error is not None:
            raise self.error
        return self.contents


def with_cgroup(files):
    """Patch Path() so only the cgroup paths named in `files` exist."""

    def fake_path(p):
        if p in files:
            return files[p]
        return FakeCgroupFile(error=FileNotFoundError(p))

    return mock.patch.object(cm, "Path", fake_path)


V2_USAGE = "/sys/fs/cgroup/memory.current"
V2_LIMIT = "/sys/fs/cgroup/memory.max"
V2_STAT = "/sys/fs/cgroup/memory.stat"
V1_USAGE = "/sys/fs/cgroup/memory/memory.usage_in_bytes"
V1_LIMIT = "/sys/fs/cgroup/memory/memory.limit_in_bytes"
V1_STAT = "/sys/fs/cgroup/memory/memory.stat"


def v2_stat(inactive_file, **extra):
    """A realistic memory.stat — the field we want is never the first line."""
    lines = ["anon 900000000", "file %d" % (inactive_file + 50_000_000), "kernel_stack 3000000"]
    lines += ["%s %d" % (k, v) for k, v in extra.items()]
    lines.append("inactive_file %d" % inactive_file)
    lines.append("slab_reclaimable 40000000")
    return FakeCgroupFile("\n".join(lines))


class ContainerMemoryTests(unittest.TestCase):
    def test_reads_usage_against_the_container_limit(self):
        used = int(CONTAINER_LIMIT_BYTES * 0.999)
        with with_cgroup({
            V2_LIMIT: FakeCgroupFile(str(CONTAINER_LIMIT_BYTES)),
            V2_USAGE: FakeCgroupFile(str(used)),
        }):
            self.assertAlmostEqual(cm.container_memory_usage_percent(), 99.9, places=1)

    def test_the_container_figure_is_nothing_like_the_host_figure(self):
        # The whole bug in one assertion: the same moment, read two ways.
        used = int(CONTAINER_LIMIT_BYTES * 0.999)
        with with_cgroup({
            V2_LIMIT: FakeCgroupFile(str(CONTAINER_LIMIT_BYTES)),
            V2_USAGE: FakeCgroupFile(str(used)),
        }):
            container = cm.container_memory_usage_percent()
        host = 100.0 * used / HOST_TOTAL_BYTES
        self.assertGreater(container, 99.0, "the container is nearly full")
        self.assertLess(host, 2.0, "yet as a share of the host it looks like nothing")

    def test_cgroup_v1_is_read_when_v2_is_absent(self):
        with with_cgroup({
            V1_LIMIT: FakeCgroupFile(str(CONTAINER_LIMIT_BYTES)),
            V1_USAGE: FakeCgroupFile(str(CONTAINER_LIMIT_BYTES // 2)),
        }):
            self.assertAlmostEqual(cm.container_memory_usage_percent(), 50.0, places=1)

    def test_no_limit_reads_as_no_limit_not_as_zero(self):
        with with_cgroup({V2_LIMIT: FakeCgroupFile("max"), V2_USAGE: FakeCgroupFile("1000")}):
            self.assertIsNone(cm.container_memory_usage_percent())

    def test_the_v1_unlimited_sentinel_is_not_a_limit(self):
        with with_cgroup({
            V1_LIMIT: FakeCgroupFile(str(1 << 63)),
            V1_USAGE: FakeCgroupFile("1000"),
        }):
            self.assertIsNone(cm.container_memory_usage_percent())

    def test_a_limit_bigger_than_the_machine_is_not_a_limit(self):
        with with_cgroup({
            V2_LIMIT: FakeCgroupFile(str(HOST_TOTAL_BYTES * 2)),
            V2_USAGE: FakeCgroupFile("1000"),
        }):
            self.assertIsNone(cm.container_memory_usage_percent())

    def test_outside_a_container_there_is_nothing_to_read(self):
        with with_cgroup({}):
            self.assertIsNone(cm.container_memory_usage_percent())

    def test_unreadable_cgroup_files_do_not_raise(self):
        with with_cgroup({
            V2_LIMIT: FakeCgroupFile(error=PermissionError("denied")),
            V2_USAGE: FakeCgroupFile("1000"),
        }):
            self.assertIsNone(cm.container_memory_usage_percent())

    def test_nonsense_in_a_cgroup_file_does_not_raise(self):
        with with_cgroup({
            V2_LIMIT: FakeCgroupFile("not-a-number"),
            V2_USAGE: FakeCgroupFile("1000"),
        }):
            self.assertIsNone(cm.container_memory_usage_percent())

    def test_usage_over_the_limit_is_clamped_to_100(self):
        with with_cgroup({
            V2_LIMIT: FakeCgroupFile(str(CONTAINER_LIMIT_BYTES)),
            V2_USAGE: FakeCgroupFile(str(CONTAINER_LIMIT_BYTES * 2)),
        }):
            self.assertEqual(cm.container_memory_usage_percent(), 100.0)


class MaxSessionPermitTests(unittest.TestCase):
    """How many pages a crawl opens at once, which is what outran the brake."""

    def setUp(self):
        self._saved = os.environ.pop("CRAWL4AI_MAX_SESSION_PERMIT", None)

    def tearDown(self):
        os.environ.pop("CRAWL4AI_MAX_SESSION_PERMIT", None)
        if self._saved is not None:
            os.environ["CRAWL4AI_MAX_SESSION_PERMIT"] = self._saved

    def _resolve(self):
        return cm.default_max_session_permit()

    def test_the_default_is_four(self):
        # 20 was the library's; 8 was our first attempt and still reached 80.3% and
        # lost Chromium. Eight of feelporto.com's pages is roughly 3 GB on its own.
        self.assertEqual(self._resolve(), 4)

    def test_the_environment_can_tune_it(self):
        os.environ["CRAWL4AI_MAX_SESSION_PERMIT"] = "6"
        self.assertEqual(self._resolve(), 6)

    def test_nonsense_falls_back_to_the_default(self):
        os.environ["CRAWL4AI_MAX_SESSION_PERMIT"] = "lots"
        self.assertEqual(self._resolve(), 4)

    def test_zero_falls_back_to_the_default(self):
        # A cap of nothing would stall every crawl rather than slow it.
        os.environ["CRAWL4AI_MAX_SESSION_PERMIT"] = "0"
        self.assertEqual(self._resolve(), 4)


class MemoryBrakeTests(unittest.TestCase):
    """Where a crawl stops opening new pages — which has to be below where it dies."""

    def setUp(self):
        self._saved = os.environ.pop("CRAWL4AI_MEMORY_THRESHOLD_PERCENT", None)

    def tearDown(self):
        os.environ.pop("CRAWL4AI_MEMORY_THRESHOLD_PERCENT", None)
        if self._saved is not None:
            os.environ["CRAWL4AI_MEMORY_THRESHOLD_PERCENT"] = self._saved

    def test_the_brake_is_below_where_chromium_actually_died(self):
        # Chromium was killed at 80.3% of the container on 2026-08-31 while the
        # library's 90% brake sat above it, never engaging.
        self.assertLess(cm.default_memory_threshold_percent(), 80.3)

    def test_the_environment_can_tune_it(self):
        os.environ["CRAWL4AI_MEMORY_THRESHOLD_PERCENT"] = "55"
        self.assertEqual(cm.default_memory_threshold_percent(), 55.0)

    def test_nonsense_falls_back_to_the_default(self):
        os.environ["CRAWL4AI_MEMORY_THRESHOLD_PERCENT"] = "loads"
        self.assertEqual(cm.default_memory_threshold_percent(), cm.DEFAULT_MEMORY_THRESHOLD_PERCENT)

    def test_an_impossible_percentage_falls_back_to_the_default(self):
        for value in ("0", "-5", "140"):
            os.environ["CRAWL4AI_MEMORY_THRESHOLD_PERCENT"] = value
            self.assertEqual(cm.default_memory_threshold_percent(), cm.DEFAULT_MEMORY_THRESHOLD_PERCENT, value)

    def test_recovery_sits_below_the_brake(self):
        # A fixed recovery point of 85 would sit ABOVE a 70 brake, and a crawl
        # hovering on the line would flap in and out of pressure mode.
        brake = cm.default_memory_threshold_percent()
        self.assertLess(brake - cm.RECOVERY_MARGIN_PERCENT, brake)
        self.assertGreater(brake - cm.RECOVERY_MARGIN_PERCENT, 0)


class ReclaimableCacheTests(unittest.TestCase):
    """
    Page cache is not memory in use.

    Regression test for 2026-09-02. `memory.current` counts file data the kernel is
    keeping only because nothing has needed the space yet. Read raw, an idle crawler
    reported 71% of its container while its own process held 1,130 MB of 4,096 MB —
    28%. The page brake sits at 70%, so a container doing nothing was already past it.

    Two crawls of feelporto.com were each handed 1,241 pages and returned 36 and 42,
    with no failures and no memory used, because the dispatcher stopped opening pages
    before it opened any. Both were rejected by the API for being too small to trust,
    so the site could not be refreshed at all — and nothing looked broken, because
    the crawls reported success.
    """

    # The live numbers, 2026-09-02.
    LIMIT = 4_096 * 1024 * 1024
    IN_USE = 1_130 * 1024 * 1024
    CACHE = 1_770 * 1024 * 1024

    def test_the_production_reading_that_stalled_every_crawl(self):
        charged = self.IN_USE + self.CACHE
        raw = 100.0 * charged / self.LIMIT
        self.assertGreater(raw, 70.0, "the raw figure must be past the brake, or this test proves nothing")

        with with_cgroup({
            V2_LIMIT: FakeCgroupFile(str(self.LIMIT)),
            V2_USAGE: FakeCgroupFile(str(charged)),
            V2_STAT: v2_stat(self.CACHE),
        }):
            self.assertAlmostEqual(cm.container_memory_usage_percent(), 27.6, places=1)

    def test_the_corrected_reading_is_below_the_brake(self):
        # The point of the whole change, stated against the threshold it has to clear.
        with with_cgroup({
            V2_LIMIT: FakeCgroupFile(str(self.LIMIT)),
            V2_USAGE: FakeCgroupFile(str(self.IN_USE + self.CACHE)),
            V2_STAT: v2_stat(self.CACHE),
        }):
            self.assertLess(cm.container_memory_usage_percent(), cm.DEFAULT_MEMORY_THRESHOLD_PERCENT)

    def test_a_container_genuinely_full_still_reads_full(self):
        # ⚠️ The dangerous direction. This brake is what stops Chromium being killed;
        # subtracting too eagerly would report a dying container as healthy.
        used = int(self.LIMIT * 0.95)
        with with_cgroup({
            V2_LIMIT: FakeCgroupFile(str(self.LIMIT)),
            V2_USAGE: FakeCgroupFile(str(used)),
            V2_STAT: v2_stat(0),
        }):
            self.assertAlmostEqual(cm.container_memory_usage_percent(), 95.0, places=1)

    def test_cgroup_v1_uses_its_own_spelling_of_the_field(self):
        # v1 calls it total_inactive_file. Reading the v2 name here would silently
        # subtract nothing and leave v1 hosts with the bug.
        charged = self.IN_USE + self.CACHE
        stat = FakeCgroupFile("cache %d\ntotal_inactive_file %d\nrss 100" % (charged, self.CACHE))
        with with_cgroup({
            V1_LIMIT: FakeCgroupFile(str(self.LIMIT)),
            V1_USAGE: FakeCgroupFile(str(charged)),
            V1_STAT: stat,
        }):
            self.assertAlmostEqual(cm.container_memory_usage_percent(), 27.6, places=1)

    def test_a_missing_breakdown_falls_back_to_the_raw_figure(self):
        # Degrades to the old behaviour rather than guessing. Not knowing how much is
        # cache must never let us claim memory is free.
        charged = self.IN_USE + self.CACHE
        with with_cgroup({
            V2_LIMIT: FakeCgroupFile(str(self.LIMIT)),
            V2_USAGE: FakeCgroupFile(str(charged)),
        }):
            self.assertAlmostEqual(cm.container_memory_usage_percent(), 100.0 * charged / self.LIMIT, places=1)

    def test_an_unreadable_or_malformed_breakdown_is_ignored(self):
        charged = self.IN_USE + self.CACHE
        expected = 100.0 * charged / self.LIMIT
        for stat in (
            FakeCgroupFile(error=PermissionError("denied")),
            FakeCgroupFile("inactive_file not-a-number"),
            FakeCgroupFile(""),
            FakeCgroupFile("anon 5\nfile 6"),
        ):
            with with_cgroup({
                V2_LIMIT: FakeCgroupFile(str(self.LIMIT)),
                V2_USAGE: FakeCgroupFile(str(charged)),
                V2_STAT: stat,
            }):
                self.assertAlmostEqual(cm.container_memory_usage_percent(), expected, places=1)

    def test_a_negative_cache_figure_cannot_inflate_the_reading(self):
        with with_cgroup({
            V2_LIMIT: FakeCgroupFile(str(self.LIMIT)),
            V2_USAGE: FakeCgroupFile(str(self.IN_USE)),
            V2_STAT: FakeCgroupFile("inactive_file -999999999"),
        }):
            self.assertAlmostEqual(cm.container_memory_usage_percent(), 100.0 * self.IN_USE / self.LIMIT, places=1)

    def test_cache_larger_than_usage_reads_as_empty_not_negative(self):
        # The two files are read a moment apart and can cross. A negative percentage
        # would sail under every brake there is.
        with with_cgroup({
            V2_LIMIT: FakeCgroupFile(str(self.LIMIT)),
            V2_USAGE: FakeCgroupFile(str(self.CACHE)),
            V2_STAT: v2_stat(self.CACHE * 2),
        }):
            self.assertEqual(cm.container_memory_usage_percent(), 0.0)

    def test_a_prefix_of_the_field_name_is_not_the_field(self):
        # "inactive_file" must not be matched by "inactive_file_something", nor
        # "file" by "inactive_file" — the v2 stat has several near-namesakes.
        with with_cgroup({
            V2_LIMIT: FakeCgroupFile(str(self.LIMIT)),
            V2_USAGE: FakeCgroupFile(str(self.IN_USE)),
            V2_STAT: FakeCgroupFile("inactive_file_foo 999999999999\nactive_file 888888888"),
        }):
            self.assertAlmostEqual(cm.container_memory_usage_percent(), 100.0 * self.IN_USE / self.LIMIT, places=1)


if __name__ == "__main__":
    unittest.main()
