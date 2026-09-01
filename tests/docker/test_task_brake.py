"""
A long crawl must give the container its processes back before it runs out.

Regression test for 2026-09-01. The memory brake was watching the wrong ceiling.
Crawling feelporto.com, one run climbed from 650 to 952 of the container's 1000
tasks over three minutes and never came down; the container then refused a fork,
Chromium died, and 107 of 248 pages were lost. Memory peaked at 2.7 GB of 4 GB
that whole time and nothing braked, because nothing was watching processes while
a crawl was in flight.

Waiting does not fix it. Chromium holds its process tree until it exits — the
count fell from 952 to 55 the instant the browser closed, and not before. So the
brake exists only to get the pages in flight down to zero, which is the one
moment the browser can safely be restarted.

Runs without crawl4ai installed: resource_limits.py is loaded straight from its
path with psutil stubbed.

    python3 -m unittest tests.docker.test_task_brake -v
"""
import os
import sys
import types
import unittest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def _install_psutil_stub():
    if "psutil" not in sys.modules:
        stub = types.ModuleType("psutil")
        stub.virtual_memory = lambda: types.SimpleNamespace(total=322_704_000_000, percent=47.9)
        sys.modules["psutil"] = stub


_install_psutil_stub()


def _load_resource_limits():
    """Compiled from source so a stale bytecode cache cannot serve the old file."""
    path = os.path.join(REPO_ROOT, "crawl4ai", "resource_limits.py")
    with open(path, encoding="utf-8") as handle:
        source = handle.read()
    module = types.ModuleType("resource_limits_under_test")
    module.__file__ = path
    exec(compile(source, path, "exec"), module.__dict__)
    return module


cm = _load_resource_limits()


class TaskBrakeTests(unittest.TestCase):
    """Where a crawl stops opening pages, for the limit that actually kills it."""

    def setUp(self):
        self._saved = os.environ.pop("CRAWL4AI_TASK_BRAKE_PERCENT", None)

    def tearDown(self):
        os.environ.pop("CRAWL4AI_TASK_BRAKE_PERCENT", None)
        if self._saved is not None:
            os.environ["CRAWL4AI_TASK_BRAKE_PERCENT"] = self._saved

    def test_the_brake_is_below_where_the_container_started_refusing_forks(self):
        # 952 of 1000 was the peak on the crawl that died; the refusals began
        # before that. The brake has to be far enough below to leave room for the
        # pages already open to finish.
        self.assertLess(cm.default_task_brake_percent(), 95.0)

    def test_the_brake_leaves_room_for_the_pages_already_in_flight(self):
        # The brake stops NEW pages; the ones open still grow. Four concurrent
        # pages cost roughly 5% of the allowance between them.
        headroom = 100.0 - cm.default_task_brake_percent()
        self.assertGreaterEqual(headroom, 20.0, "not enough left for the pages still finishing")

    def test_the_brake_sits_above_the_recycle_point(self):
        # The order matters and is the whole mechanism: the brake drains the
        # crawl, and the restart then fires because usage is still above the
        # lower recycle line. A brake below it could never trigger a restart.
        self.assertGreater(cm.default_task_brake_percent(), cm.default_task_recycle_percent())

    def test_the_environment_can_tune_it(self):
        os.environ["CRAWL4AI_TASK_BRAKE_PERCENT"] = "55"
        self.assertEqual(cm.default_task_brake_percent(), 55.0)

    def test_nonsense_falls_back_to_the_default(self):
        os.environ["CRAWL4AI_TASK_BRAKE_PERCENT"] = "loads"
        self.assertEqual(cm.default_task_brake_percent(), cm.TASK_BRAKE_PERCENT)

    def test_an_impossible_percentage_falls_back_to_the_default(self):
        for value in ("0", "-5", "140"):
            os.environ["CRAWL4AI_TASK_BRAKE_PERCENT"] = value
            self.assertEqual(cm.default_task_brake_percent(), cm.TASK_BRAKE_PERCENT, value)


class BrakeHoldTests(unittest.TestCase):
    """The brake must have a way out, or a crawl hangs instead of crashing."""

    def test_the_brake_cannot_be_held_forever(self):
        # The restart it is waiting for needs zero open pages, and a browser
        # shared with a second crawl may never reach that. A brake with no
        # release would hang the crawl silently — worse than the crash, because
        # nothing times out and nobody is told.
        self.assertGreater(cm.TASK_BRAKE_MAX_HOLD_SEC, 0)
        self.assertLessEqual(cm.TASK_BRAKE_MAX_HOLD_SEC, 600.0, "a hold this long is a hang")

    def test_giving_up_holds_the_brake_off_long_enough_to_get_work_done(self):
        # Releasing alone achieves nothing: the usage that put the brake on is
        # still there, so the monitor re-engages on its next reading and the crawl
        # releases and re-brakes forever without opening a single page. Caught by
        # the modelled cycle in test_mid_crawl_recycle before it ever shipped.
        self.assertGreaterEqual(cm.TASK_BRAKE_COOLDOWN_SEC, cm.TASK_BRAKE_MAX_HOLD_SEC)

    def test_the_hold_outlasts_a_page(self):
        # Releasing before the pages in flight have finished would defeat the
        # drain the brake exists to produce.
        self.assertGreaterEqual(cm.TASK_BRAKE_MAX_HOLD_SEC, 60.0)


if __name__ == "__main__":
    unittest.main()
