"""
The browser must be restarted mid-crawl — but only when nothing is open in it.

Regression test for 2026-09-01. Chromium gives its processes back only by
exiting: a crawl of feelporto.com climbed from 650 to 952 of the container's
1000 tasks over three minutes, held there, and died on a refused fork, losing
107 of 248 pages. Closing the pages changed nothing; closing the browser dropped
it to 55 instantly. So a long crawl has to stop, let the pages in flight finish,
and relaunch the browser partway through.

The danger in that is the Docker pool, which hands ONE browser to every crawl
sharing a config. Restarting it while another crawl has a page open would kill
that crawl to save this one — the same "handed a corpse" failure the liveness
work was about, only self-inflicted. Zero open pages is the only safe moment,
and "cannot tell" is not zero.

    python3 -m unittest tests.docker.test_mid_crawl_recycle -v
"""
import os
import sys
import types
import unittest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def _load_resource_limits():
    """Compiled from source — see test_resource_limits for why not imported."""
    if "psutil" not in sys.modules:
        stub = types.ModuleType("psutil")
        stub.virtual_memory = lambda: types.SimpleNamespace(total=322_704_000_000, percent=47.9)
        sys.modules["psutil"] = stub
    path = os.path.join(REPO_ROOT, "crawl4ai", "resource_limits.py")
    with open(path, encoding="utf-8") as handle:
        source = handle.read()
    module = types.ModuleType("resource_limits_under_test")
    module.__file__ = path
    exec(compile(source, path, "exec"), module.__dict__)
    return module


limits = _load_resource_limits()

STARVED = 80.0  # well past the recycle line
QUIET = 5.0  # a container with nothing running


def may(task_percent, pages_in_use, owns_browser=True, has_sessions=False):
    return limits.may_recycle_browser(
        task_percent,
        pages_in_use,
        owns_browser=owns_browser,
        has_sessions=has_sessions,
    )


class RecycleDecisionTests(unittest.TestCase):
    def test_a_starved_container_with_nothing_open_is_restarted(self):
        self.assertTrue(may(STARVED, 0))

    def test_a_page_still_in_use_is_never_closed_underneath_its_crawl(self):
        # The shared-browser case: this crawl has drained, another has not.
        self.assertFalse(may(STARVED, 1))

    def test_a_browser_we_cannot_inspect_is_left_alone(self):
        # None is not zero. crawl4ai's internals move, and guessing "nothing in
        # use" wrong costs somebody else's page.
        self.assertFalse(may(STARVED, None))

    def test_the_page_headless_mode_never_closes_does_not_block_the_restart(self):
        """
        The count is pages IN USE, not pages that exist.

        Measured 2026-09-01 with the brake forced low enough to fire: it engaged
        at 31%, waited its full two minutes, and gave up having restarted
        nothing. In headless mode crawl4ai deliberately leaves the last page open
        rather than closing it — `total_pages <= 1 ... pass` in
        _crawl_web's finally — so a browser at rest still holds one page object
        and a check against that number can never reach zero.

        A crawl that has finished with every page it took must be recyclable even
        though the browser is not empty.
        """
        pages_that_exist, pages_checked_out = 1, 0
        self.assertTrue(may(STARVED, pages_checked_out))
        self.assertFalse(
            may(STARVED, pages_that_exist),
            "counting page objects instead of checkouts is what wedged it",
        )

    def test_a_quiet_container_is_left_alone(self):
        # Relaunching Chromium costs seconds off every crawl that follows, for
        # nothing, when there is no pressure to relieve.
        self.assertFalse(may(QUIET, 0))

    def test_an_uncapped_container_is_left_alone(self):
        # No cgroup limit to read: off Railway, on a laptop, in CI. There is no
        # ceiling to run into, so there is nothing to protect against.
        self.assertFalse(may(None, 0))

    def test_a_browser_we_did_not_launch_is_not_ours_to_kill(self):
        self.assertFalse(may(STARVED, 0, owns_browser=False))

    def test_a_session_pins_the_page_it_owns(self):
        # A session promises the same page back on the next call.
        self.assertFalse(may(STARVED, 0, has_sessions=True))

    def test_exactly_at_the_line_counts_as_starved(self):
        self.assertTrue(may(limits.default_task_recycle_percent(), 0))

    def test_just_below_the_line_does_not(self):
        self.assertFalse(may(limits.default_task_recycle_percent() - 0.1, 0))

    def test_the_threshold_can_be_overridden_per_call(self):
        self.assertTrue(may_at(50.0, 40.0))
        self.assertFalse(may_at(50.0, 60.0))


def may_at(task_percent, threshold):
    return limits.may_recycle_browser(
        task_percent, 0, owns_browser=True, has_sessions=False, threshold=threshold
    )


class DrainThenRestartTests(unittest.TestCase):
    """
    The cycle the dispatcher runs: brake, drain, restart, release.

    Modelled rather than imported — async_dispatcher pulls in the whole of
    crawl4ai — so this asserts the ordering, against the real constants and the
    real decision function.
    """

    def setUp(self):
        self.tasks = 65.0  # of the container's allowance
        self.pages_in_flight = 4
        self.braked = False
        self.braked_since = None
        self.restarts = 0
        self.pages_crawled = 0
        self.suppressed_until = 0.0
        self.now = 0.0

    def tick(self, seconds=1.0):
        self.now += seconds

        # The monitor: brake on above the line, off below it — unless the brake
        # has just given up waiting for a restart and is being held off.
        if self.tasks >= limits.default_task_brake_percent() and self.now >= self.suppressed_until:
            if not self.braked:
                self.braked = True
                self.braked_since = self.now
        elif self.braked:
            self.braked, self.braked_since = False, None

        # The scheduler: braked means no new pages, so the ones open drain away.
        if self.braked and self.pages_in_flight:
            self.pages_in_flight -= 1
            return

        # Drained and still braked — the one moment a restart is safe.
        if self.braked and not self.pages_in_flight:
            if limits.may_recycle_browser(
                self.tasks, 0, owns_browser=True, has_sessions=False
            ):
                self.restarts += 1
                self.tasks = QUIET  # Chromium hands the processes back on exit
                self.braked, self.braked_since = False, None
                return
            if self.now - self.braked_since >= limits.TASK_BRAKE_MAX_HOLD_SEC:
                self.suppressed_until = self.now + limits.TASK_BRAKE_COOLDOWN_SEC
                self.braked, self.braked_since = False, None
            return

        # Crawling normally: every page costs a little more of the allowance.
        self.tasks = min(100.0, self.tasks + 1.0)
        self.pages_in_flight = 4
        self.pages_crawled += 4

    def test_a_climbing_crawl_is_rescued_before_it_runs_out(self):
        for _ in range(200):
            self.tick()
            self.assertLess(self.tasks, 95.0, "the ceiling was reached anyway")
        self.assertGreater(self.restarts, 0, "the browser was never restarted")

    def test_the_pages_in_flight_finish_before_the_restart(self):
        self.tasks = limits.default_task_brake_percent()
        self.tick()
        self.assertTrue(self.braked)
        while self.pages_in_flight:
            self.assertEqual(self.restarts, 0, "restarted with pages still open")
            self.tick()
        self.tick()
        self.assertEqual(self.restarts, 1)

    def test_the_brake_comes_off_once_the_processes_are_back(self):
        self.tasks = limits.default_task_brake_percent()
        for _ in range(10):
            self.tick()
        self.assertFalse(self.braked, "the crawl must start again after the restart")

    def test_a_restart_that_never_becomes_possible_does_not_hang_the_crawl(self):
        # A browser shared with a second crawl that keeps pages open: this crawl
        # drains, but the browser never has zero pages, so the restart never
        # happens. Hanging here would be worse than the crash — nothing times
        # out and nobody is told.
        self.tasks = limits.default_task_brake_percent()
        self.tick()
        self.assertTrue(self.braked)

        # Pretend the browser can never be inspected as empty.
        limits_may = limits.may_recycle_browser
        limits.may_recycle_browser = lambda *a, **k: False
        try:
            for _ in range(int(limits.TASK_BRAKE_MAX_HOLD_SEC) + 20):
                self.tick()
        finally:
            limits.may_recycle_browser = limits_may

        self.assertEqual(self.restarts, 0, "there was never a safe moment to restart")
        self.assertGreater(
            self.pages_crawled,
            0,
            "the crawl read nothing at all — the brake wedged it instead of slowing it",
        )


if __name__ == "__main__":
    unittest.main()
