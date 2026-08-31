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
import importlib.util
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
    """Load the module by path, so `crawl4ai/__init__` is not dragged in with it."""
    path = os.path.join(REPO_ROOT, "crawl4ai", "resource_limits.py")
    spec = importlib.util.spec_from_file_location("resource_limits_under_test", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
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
V1_USAGE = "/sys/fs/cgroup/memory/memory.usage_in_bytes"
V1_LIMIT = "/sys/fs/cgroup/memory/memory.limit_in_bytes"


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

    def test_the_default_is_eight_not_twenty(self):
        self.assertEqual(self._resolve(), 8, "20 heavy pages at once is the whole container")

    def test_the_environment_can_tune_it(self):
        os.environ["CRAWL4AI_MAX_SESSION_PERMIT"] = "4"
        self.assertEqual(self._resolve(), 4)

    def test_nonsense_falls_back_to_the_default(self):
        os.environ["CRAWL4AI_MAX_SESSION_PERMIT"] = "lots"
        self.assertEqual(self._resolve(), 8)

    def test_zero_falls_back_to_the_default(self):
        # A cap of nothing would stall every crawl rather than slow it.
        os.environ["CRAWL4AI_MAX_SESSION_PERMIT"] = "0"
        self.assertEqual(self._resolve(), 8)


if __name__ == "__main__":
    unittest.main()
