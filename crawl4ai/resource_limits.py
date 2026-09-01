"""
The limits this process actually runs under: how much memory it may use, how many
pages it may open at once, and how many processes and threads it may create.

Kept in its own module, free of every crawl4ai import, because both the library
and the Docker server need to read them and neither should have to import the
other to do it. Two copies of the memory question is what caused the outage this
module exists to prevent.

`psutil` reports the MACHINE. Inside a container those are wildly different
numbers — on Railway psutil sees a 322.7 GB host while the container is held to
3.7 GB — and a memory guard pointed at the wrong one cannot fire.
"""
import os
import psutil
from pathlib import Path
from typing import Optional

_CGROUP_MEMORY_PATHS = (
    # cgroup v2
    ("/sys/fs/cgroup/memory.current", "/sys/fs/cgroup/memory.max"),
    # cgroup v1
    ("/sys/fs/cgroup/memory/memory.usage_in_bytes", "/sys/fs/cgroup/memory/memory.limit_in_bytes"),
)

_CGROUP_TASK_PATHS = (
    # cgroup v2
    ("/sys/fs/cgroup/pids.current", "/sys/fs/cgroup/pids.max"),
    # cgroup v1
    ("/sys/fs/cgroup/pids/pids.current", "/sys/fs/cgroup/pids/pids.max"),
)

# cgroup v1 spells "no limit" as an enormous sentinel rather than a word.
_CGROUP_UNLIMITED = 1 << 62


def container_memory_usage_percent() -> Optional[float]:
    """
    Memory used as a share of the container's limit, or None if there is no limit.

    None means "not held to a container limit", not "zero" — callers fall back to
    the machine's own figures.
    """
    for usage_path, limit_path in _CGROUP_MEMORY_PATHS:
        try:
            limit_text = Path(limit_path).read_text().strip()
            if limit_text == "max":
                return None
            limit = int(limit_text)
            usage = int(Path(usage_path).read_text().strip())
        except (OSError, ValueError):
            continue

        if limit <= 0 or limit >= _CGROUP_UNLIMITED:
            return None
        # A limit at or above the machine's own memory is not a limit.
        if limit >= psutil.virtual_memory().total:
            return None

        return max(0.0, min(100.0, 100.0 * usage / limit))

    return None


def container_task_usage_percent() -> Optional[float]:
    """
    Processes and threads in use as a share of the container's limit, or None if
    there is no limit.

    ⚠️ This counts THREADS, not processes — the cgroup `pids` controller limits
    tasks, and every thread is a task. It is the limit that actually bites when
    crawling, and it is invisible if you only watch memory.

    Measured on Railway 2026-09-01, crawling 120 pages of one site:

        memory   1.7 GB of 6.0 GB      (28%)  — nowhere near the limit
        tasks    798 of 1000           (80%)  — and it stayed there when idle

    Chromium is a process tree. A crawl leaves renderer processes behind, and 28 of
    them were still resident with no crawl running, holding 725 threads between
    them. The next crawl therefore starts with a fifth of the ceiling left, hits it
    mid-run, and `clone()` starts failing: Python raises "can't start new thread",
    the Playwright driver's pipe closes, and every page still queued dies with
    "Connection closed while reading from the driver". A 500-page crawl of
    feelporto.com lost 435 pages that way while memory sat at 30%.
    """
    for usage_path, limit_path in _CGROUP_TASK_PATHS:
        try:
            limit_text = Path(limit_path).read_text().strip()
            if limit_text == "max":
                return None
            limit = int(limit_text)
            usage = int(Path(usage_path).read_text().strip())
        except (OSError, ValueError):
            continue

        if limit <= 0 or limit >= _CGROUP_UNLIMITED:
            return None

        return max(0.0, min(100.0, 100.0 * usage / limit))

    return None


# The share of the container's process/thread allowance past which a browser is
# recycled rather than reused.
#
# Below where things break, with room for a crawl to finish: the ceiling is hit
# during a run, not at the start, so the check has to leave enough headroom for
# whatever the next run will spawn. At 798 of 1000 idle, an incoming crawl had
# about 200 tasks to play with and needed roughly 740.
TASK_RECYCLE_PERCENT = 60.0


def default_task_recycle_percent() -> float:
    """The task-usage share past which browsers are recycled, env override first."""
    raw = os.environ.get("CRAWL4AI_TASK_RECYCLE_PERCENT")
    if raw:
        try:
            value = float(raw)
        except ValueError:
            return TASK_RECYCLE_PERCENT
        if 0 < value <= 100:
            return value
    return TASK_RECYCLE_PERCENT


# The share of the container's process/thread allowance at which a crawl stops
# opening new pages.
#
# The sibling of the memory brake below, for the limit that actually bites.
# Chromium does not hand processes back while it is running: measured on Railway
# 2026-09-01, one crawl of feelporto.com climbed from 650 to 952 of 1000 tasks in
# three minutes and never came down, until the container refused a fork and the
# browser died. 107 of 248 pages were lost. Memory that whole time peaked at
# 2.7 GB of 4 GB and never triggered anything.
#
# Braking alone would only stall - the processes are held by the browser, not by
# the pages in flight, so waiting does not release them. The brake exists to get
# the open pages down to zero, which is the one moment a browser can be restarted
# safely; BrowserManager.recycle_if_process_starved does the restarting.
TASK_BRAKE_PERCENT = 70.0

# How long the task brake may stay on before it gives up and lets pages through.
#
# The brake clears when the browser restarts, and that can only happen when no
# pages are open anywhere in it. A browser shared with a second crawl may never
# reach that moment, and a brake with no way out would hang the crawl for good --
# which is worse than the crash it is preventing, because nothing times out and
# nobody is told. So it is a brake, not a gate: after this long it releases,
# loudly.
TASK_BRAKE_MAX_HOLD_SEC = 120.0


# How long the task brake stays off after it has given up waiting for a restart.
#
# Releasing alone is not enough. The usage that put the brake on is still there -
# the restart is what clears it - so the monitor re-engages on its very next
# reading and the crawl goes straight back to a standstill, releasing and
# re-braking forever without opening a page. It has to be held off long enough
# for the crawl to actually get some work done.
TASK_BRAKE_COOLDOWN_SEC = 120.0


def default_task_brake_percent() -> float:
    """The task-usage share at which a crawl stops opening pages, env override first."""
    raw = os.environ.get("CRAWL4AI_TASK_BRAKE_PERCENT")
    if raw:
        try:
            value = float(raw)
        except ValueError:
            return TASK_BRAKE_PERCENT
        if 0 < value <= 100:
            return value
    return TASK_BRAKE_PERCENT


def may_recycle_browser(
    task_percent: Optional[float],
    pages_in_use: Optional[int],
    *,
    owns_browser: bool,
    has_sessions: bool,
    threshold: Optional[float] = None,
) -> bool:
    """
    Whether a browser may be closed and relaunched right now to free processes.

    Every "no" here is something that went wrong once, or would:

    - `pages_in_use` above zero means a crawl is mid-page in this browser. On the
      Docker server one browser is shared by every crawl with the same config, so
      that crawl may not be the one asking. Nothing checked out is the only
      moment the answer is safe, and it is why the brake drains the crawl first.

      ⚠️ Pages IN USE, not pages that exist. In headless mode crawl4ai leaves the
      last page open on purpose rather than closing it, so the browser is never
      empty of page objects. Gating on that count instead, the brake engaged,
      waited its full two minutes and gave up having restarted nothing - measured
      2026-09-01 with the threshold forced low enough to trigger.
    - `pages_in_use` of None means there is no browser to speak of, or its state
      could not be read. Not knowing is a reason to leave it alone.
    - `owns_browser` false: a browser reached over CDP, or one a managed-browser
      process is running, was not ours to launch and is not ours to kill.
    - `has_sessions` true: a session pins a page the caller expects to still be
      there on its next call.
    - Below the threshold there is nothing to relieve, and relaunching Chromium
      costs a good few seconds of every crawl that follows.
    """
    if not owns_browser or has_sessions:
        return False
    if pages_in_use is None or pages_in_use > 0:
        return False
    if task_percent is None:
        return False
    return task_percent >= (threshold if threshold is not None else default_task_recycle_percent())


# How many pages may be open at once when nobody says otherwise.
#
# A deep crawl never passes a dispatcher — BFSDeepCrawlStrategy hands a whole level
# of URLs to `arun_many`, which builds a MemoryAdaptiveDispatcher with its defaults
# — so this number alone decides how many Chromium tabs a crawl of a website opens
# together. `crawler.pool.max_pages` does NOT cap it: that limits concurrent arun()
# calls, and a deep crawl is one arun call however many pages it reads.
#
# Twenty heavy pages at once needs roughly 4 GB, which is the entire container. On
# 2026-08-31 that took feelporto.com from 10% memory to 99.9% in two minutes and
# Chromium was killed.
#
# Eight was the first attempt at a safe number and was still too many: re-running
# the same crawl reached 80.3% and Chromium was killed again — better, in that the
# crawl finished and the pool healed itself, but 65 of 207 pages were lost. Eight
# of that site's pages is roughly 3 GB on its own. Four leaves the brake below
# room to work in.
DEFAULT_MAX_SESSION_PERMIT = 4


def default_max_session_permit() -> int:
    """
    The page-concurrency cap, tunable without a code change.

    The Docker server publishes `crawler.pool.max_session_permit` from config.yml
    into the environment at startup, which keeps config.yml the single place this
    is set even though the library is what reads it.
    """
    try:
        configured = int(os.environ.get("CRAWL4AI_MAX_SESSION_PERMIT", ""))
    except ValueError:
        return DEFAULT_MAX_SESSION_PERMIT
    return configured if configured > 0 else DEFAULT_MAX_SESSION_PERMIT


# The share of the container at which a crawl stops opening new pages.
#
# Not a target — a brake. Above this the dispatcher adds no further pages and lets
# the ones in flight finish, so memory comes back down instead of climbing into an
# OOM kill.
#
# 90 was the library's default and it is above where things actually break: on
# 2026-08-31 Chromium was killed at 80.3% of the container, so the brake never
# engaged at all. 70 leaves roughly a gigabyte of headroom for pages that are
# already open, which is what has to fit — the guard checks once a second, and
# memory climbed about half a percent a second at twenty pages.
DEFAULT_MEMORY_THRESHOLD_PERCENT = 70.0

# How far memory must fall before a crawl starts opening pages again. Below the
# brake, so a crawl hovering exactly at the line does not flap in and out of it.
RECOVERY_MARGIN_PERCENT = 10.0

# How long the brake may stay on once memory is back below it.
#
# The recovery point is a guess at a number the container can actually reach, and
# it can be wrong: a browser at rest holds memory too. On 2026-08-31 a brake at 70%
# released at 60%, and a crawl of feelporto.com settled at 61.6% — below the brake,
# above the release, with nothing running to bring it any lower. The brake stayed on
# and the crawl waited for a number that could never arrive. It was still waiting
# twelve minutes later, having read nothing.
#
# So the release point is a preference, not a condition. Once memory has been below
# the brake this long, the brake comes off whether or not it was reached.
PRESSURE_RELEASE_SEC = 30.0


def default_memory_threshold_percent() -> float:
    """The brake, tunable without a code change — see default_max_session_permit."""
    try:
        configured = float(os.environ.get("CRAWL4AI_MEMORY_THRESHOLD_PERCENT", ""))
    except ValueError:
        return DEFAULT_MEMORY_THRESHOLD_PERCENT
    if 0 < configured <= 100:
        return configured
    return DEFAULT_MEMORY_THRESHOLD_PERCENT
