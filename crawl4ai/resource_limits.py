"""
The limits this process actually runs under: how much memory it may use, and how
many pages it may open at once.

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
# Chromium was killed. Eight leaves room for a crawl to be braked rather than shot:
# the memory guard checks once a second, and memory climbed about half a percent a
# second at twenty pages.
DEFAULT_MAX_SESSION_PERMIT = 8


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
