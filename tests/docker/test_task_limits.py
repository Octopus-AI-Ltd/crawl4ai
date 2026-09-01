"""
The resource a crawl actually runs out of is PROCESSES, not memory.

Regression test for 2026-09-01. A 500-page crawl of feelporto.com lost 435 pages.
Every one failed with

    BrowserContext.new_page: Connection closed while reading from the driver

preceded in the server log by `asyncio - WARNING - pipe closed by peer`. That is
the Playwright driver process dying, and it died because the container ran out of
tasks, not memory. Measured live on Railway while crawling 120 pages of one site:

    memory   1.7 GB of 6.0 GB   (28%)
    tasks     798 of 1000       (80%)

and when the crawl finished and nothing at all was running:

    pids.current = 798      28 chrome processes holding 725 threads

Chromium keeps its renderers after the pages are closed, and only gives them back
when the browser exits. So the next crawl starts with a fifth of the ceiling left,
hits it mid-run, `clone()` fails, and every page still queued dies at once.

Raising the container from 3.7 GB to 6 GB changed nothing, because memory was never
the limit being hit.

Runs without crawl4ai installed.

    python3 -m unittest tests.docker.test_task_limits -v
"""
import os
import sys
import types
import unittest
from unittest import mock

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# Matches test_resource_limits.py: a shared stub, and the same host total, so the
# two suites cannot make each other's container limits stop looking like limits.
HOST_TOTAL_BYTES = 322_704_000_000


def _install_psutil_stub():
    if "psutil" not in sys.modules:
        stub = types.ModuleType("psutil")
        stub.virtual_memory = lambda: types.SimpleNamespace(total=HOST_TOTAL_BYTES, percent=47.9)
        sys.modules["psutil"] = stub


_install_psutil_stub()


def _load_resource_limits():
    """Compile from source; a bytecode cache will serve a stale copy (see test_resource_limits)."""
    path = os.path.join(REPO_ROOT, "crawl4ai", "resource_limits.py")
    with open(path, encoding="utf-8") as handle:
        source = handle.read()
    module = types.ModuleType("resource_limits_task_tests")
    module.__file__ = path
    exec(compile(source, path, "exec"), module.__dict__)
    return module


rl = _load_resource_limits()


class FakeCgroupFile:
    def __init__(self, contents=None, error=None):
        self.contents = contents
        self.error = error

    def read_text(self):
        if self.error is not None:
            raise self.error
        return self.contents


def with_cgroup(files):
    def fake_path(p):
        if p in files:
            return files[p]
        return FakeCgroupFile(error=FileNotFoundError(p))

    return mock.patch.object(rl, "Path", fake_path)


V2_CUR = "/sys/fs/cgroup/pids.current"
V2_MAX = "/sys/fs/cgroup/pids.max"
V1_CUR = "/sys/fs/cgroup/pids/pids.current"
V1_MAX = "/sys/fs/cgroup/pids/pids.max"

MEM_CUR = "/sys/fs/cgroup/memory.current"
MEM_MAX = "/sys/fs/cgroup/memory.max"


class ContainerTaskTests(unittest.TestCase):
    def test_reads_tasks_against_the_container_limit(self):
        with with_cgroup({V2_MAX: FakeCgroupFile("1000"), V2_CUR: FakeCgroupFile("798")}):
            self.assertAlmostEqual(rl.container_task_usage_percent(), 79.8, places=1)

    def test_the_measurement_that_memory_could_not_make(self):
        # The whole bug in one assertion: the moment the crawl died, read both ways.
        # Memory says there is nothing wrong. Tasks say we are about to fall over.
        files = {
            MEM_MAX: FakeCgroupFile("6000000000"),
            MEM_CUR: FakeCgroupFile("1700000000"),
            V2_MAX: FakeCgroupFile("1000"),
            V2_CUR: FakeCgroupFile("798"),
        }
        with with_cgroup(files):
            self.assertAlmostEqual(rl.container_memory_usage_percent(), 28.3, places=1)
            self.assertAlmostEqual(rl.container_task_usage_percent(), 79.8, places=1)
            self.assertLess(rl.container_memory_usage_percent(), rl.default_memory_threshold_percent())
            self.assertGreater(rl.container_task_usage_percent(), rl.default_task_recycle_percent())

    def test_reads_cgroup_v1_when_v2_is_absent(self):
        with with_cgroup({V1_MAX: FakeCgroupFile("1000"), V1_CUR: FakeCgroupFile("500")}):
            self.assertAlmostEqual(rl.container_task_usage_percent(), 50.0, places=1)

    def test_no_limit_is_not_zero_usage(self):
        # None means "not held to a limit"; 0.0 would read as "plenty of room" and
        # is exactly as wrong when it is not true.
        with with_cgroup({V2_MAX: FakeCgroupFile("max"), V2_CUR: FakeCgroupFile("798")}):
            self.assertIsNone(rl.container_task_usage_percent())

    def test_no_cgroup_at_all_is_not_a_limit(self):
        with with_cgroup({}):
            self.assertIsNone(rl.container_task_usage_percent())

    def test_a_nonsense_limit_is_not_a_limit(self):
        for value in ("0", "-1", str(1 << 62)):
            with self.subTest(value=value):
                with with_cgroup({V2_MAX: FakeCgroupFile(value), V2_CUR: FakeCgroupFile("798")}):
                    self.assertIsNone(rl.container_task_usage_percent())

    def test_unreadable_files_do_not_raise(self):
        with with_cgroup({V2_MAX: FakeCgroupFile(error=PermissionError()), V2_CUR: FakeCgroupFile("798")}):
            self.assertIsNone(rl.container_task_usage_percent())

    def test_garbage_contents_do_not_raise(self):
        with with_cgroup({V2_MAX: FakeCgroupFile("not-a-number"), V2_CUR: FakeCgroupFile("798")}):
            self.assertIsNone(rl.container_task_usage_percent())


class RecycleThresholdTests(unittest.TestCase):
    def test_leaves_headroom_for_a_whole_crawl(self):
        # The ceiling is hit DURING a run, so the threshold has to leave room for
        # everything the next run will spawn - about 740 tasks, measured. Checking
        # only for "nearly full" would let a crawl start with 200 tasks left and
        # die a minute later, which is what happened.
        self.assertLessEqual(rl.default_task_recycle_percent(), 65.0)
        self.assertGreater(rl.default_task_recycle_percent(), 0)

    def test_the_observed_idle_reading_would_trigger_a_recycle(self):
        # 798 of 1000, with nothing running. This is the state that must never be
        # handed to the next crawl.
        self.assertGreater(79.8, rl.default_task_recycle_percent())

    def test_env_override(self):
        with mock.patch.dict(os.environ, {"CRAWL4AI_TASK_RECYCLE_PERCENT": "45"}):
            self.assertEqual(rl.default_task_recycle_percent(), 45.0)

    def test_nonsense_env_falls_back(self):
        for value in ("", "abc", "0", "-5", "150"):
            with self.subTest(value=value):
                with mock.patch.dict(os.environ, {"CRAWL4AI_TASK_RECYCLE_PERCENT": value}):
                    self.assertEqual(rl.default_task_recycle_percent(), rl.TASK_RECYCLE_PERCENT)


if __name__ == "__main__":
    unittest.main(verbosity=2)
