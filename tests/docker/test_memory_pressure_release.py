"""
The brake must come off again, even if memory never reaches the release point.

Regression test for 2026-08-31. The dispatcher stops opening new pages above
`memory_threshold_percent` and starts again below `recovery_threshold_percent`.
Set to 70% and 60%, a crawl of feelporto.com settled at **61.6%** — below the
brake, above the release, with nothing running to bring it any lower, because a
browser at rest holds memory too.

Nothing was above the brake, so nothing re-entered pressure mode. Nothing was
below the release, so nothing left it. `memory_wait_timeout` did not fire either:
it only counts time spent ABOVE the threshold. The crawl sat in pressure mode
refusing to open a single page, and was still sitting there twelve minutes later
having read nothing. Not a crash — a crawl that simply never finishes.

The release point is now a preference rather than a condition: once memory has
been below the brake for PRESSURE_RELEASE_SEC, the brake comes off regardless.

Modelled rather than imported — async_dispatcher pulls in the whole of crawl4ai —
so this asserts the state machine, against the real constants.

    python3 -m unittest tests.docker.test_memory_pressure_release -v
"""
import os
import types
import unittest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def _load_resource_limits():
    """Compiled from source — see test_resource_limits for why not imported."""
    import sys

    if "psutil" not in sys.modules:
        # Same figures as test_resource_limits. Whichever test module loads first
        # installs the stub for both, so they must agree or the other one's
        # container limits stop looking like limits at all.
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


class Brake:
    """The dispatcher's pressure state machine, driven by a clock we control."""

    def __init__(self, threshold, recovery, release_after):
        self.threshold = threshold
        self.recovery = recovery
        self.release_after = release_after
        self.pressure = False
        self._below_since = None
        self.now = 0.0

    def tick(self, memory_percent, seconds=1.0):
        self.now += seconds
        if memory_percent >= self.threshold:
            self._below_since = None
            self.pressure = True
        elif self.pressure and memory_percent <= self.recovery:
            self._release()
        elif self.pressure:
            if self._below_since is None:
                self._below_since = self.now
            elif self.now - self._below_since >= self.release_after:
                self._release()
        return self.pressure

    def _release(self):
        self.pressure = False
        self._below_since = None

    @property
    def opening_pages(self):
        return not self.pressure


def brake_at(threshold=70.0, recovery=None, release_after=None):
    return Brake(
        threshold,
        recovery if recovery is not None else threshold - limits.RECOVERY_MARGIN_PERCENT,
        release_after if release_after is not None else limits.PRESSURE_RELEASE_SEC,
    )


class PressureReleaseTests(unittest.TestCase):
    def test_the_brake_goes_on_above_the_threshold(self):
        brake = brake_at()
        brake.tick(75.7)
        self.assertFalse(brake.opening_pages)

    def test_the_brake_comes_off_at_the_release_point(self):
        brake = brake_at()
        brake.tick(75.7)
        brake.tick(59.0)
        self.assertTrue(brake.opening_pages)

    def test_memory_stuck_between_the_two_does_not_wedge_the_crawl(self):
        """The exact production failure: brake 70, release 60, memory 61.6."""
        brake = brake_at()
        brake.tick(75.7)
        self.assertFalse(brake.opening_pages, "the brake should go on")

        # Twelve minutes of the reading that was actually in the logs.
        for _ in range(int(12 * 60)):
            brake.tick(61.6)

        self.assertTrue(brake.opening_pages, "the crawl must not wait forever for a number it cannot reach")

    def test_the_brake_holds_for_the_release_window_first(self):
        # It comes off on time, not instantly — a crawl that drops below the brake
        # for one reading has not recovered.
        brake = brake_at()
        brake.tick(75.7)
        for _ in range(int(limits.PRESSURE_RELEASE_SEC) - 2):
            brake.tick(61.6)
        self.assertFalse(brake.opening_pages, "released too early to be hysteresis at all")

        for _ in range(5):
            brake.tick(61.6)
        self.assertTrue(brake.opening_pages, "but it must come off shortly after the window")

    def test_going_back_over_the_threshold_restarts_the_window(self):
        brake = brake_at()
        brake.tick(75.7)
        for _ in range(int(limits.PRESSURE_RELEASE_SEC) - 5):
            brake.tick(61.6)
        brake.tick(72.0)  # back over the brake
        for _ in range(int(limits.PRESSURE_RELEASE_SEC) - 5):
            brake.tick(61.6)
        self.assertFalse(brake.opening_pages, "the window must start again, not carry on from before")

    def test_a_crawl_that_never_gets_hot_never_brakes(self):
        brake = brake_at()
        for _ in range(100):
            brake.tick(40.0)
        self.assertTrue(brake.opening_pages)

    def test_the_release_window_is_short_enough_to_matter(self):
        # Long enough to be hysteresis, short enough that a wedged crawl recovers
        # in seconds rather than outliving the crawl it was meant to protect.
        self.assertGreaterEqual(limits.PRESSURE_RELEASE_SEC, 5)
        self.assertLessEqual(limits.PRESSURE_RELEASE_SEC, 120)


if __name__ == "__main__":
    unittest.main()
