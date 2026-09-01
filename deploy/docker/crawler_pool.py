# crawler_pool.py - Smart browser pool with tiered management
import asyncio, json, hashlib, time
from contextlib import suppress
from typing import Dict, Optional
from crawl4ai import AsyncWebCrawler, BrowserConfig
from utils import load_config, get_container_memory_percent, get_container_task_percent
import logging

logger = logging.getLogger(__name__)
CONFIG = load_config()

# Pool tiers
PERMANENT: Optional[AsyncWebCrawler] = None  # Shared default browser (started lazily, closed when idle)
DEFAULT_BROWSER_CONFIG: Optional[BrowserConfig] = None  # Registered at startup, used for lazy start
HOT_POOL: Dict[str, AsyncWebCrawler] = {}    # Frequent configs
COLD_POOL: Dict[str, AsyncWebCrawler] = {}   # Rare configs
LAST_USED: Dict[str, float] = {}
USAGE_COUNT: Dict[str, int] = {}
LOCK = asyncio.Lock()

# How many crawls are running on each browser right now, and when the oldest of
# them started.
#
# The janitor measures idleness from the moment a browser was handed OUT, and
# nothing re-stamped it while the browser was working. A deep crawl asks for one
# browser and then works for minutes, so a long crawl looked idle for its whole
# duration. On 2026-08-24 that reaped the browser out from under a running crawl
# of week2week.co.uk after 313 seconds — just past the 300s cold-pool TTL. The
# 292 pages still queued then failed instantly with "'NoneType' object has no
# attribute 'new_context'", the job still reported `completed`, and 68 pages
# silently replaced a 223-page site.
#
# So a browser is only idle once nothing is running on it. `LAST_USED` is now
# also re-stamped as work finishes, which is what the TTL should have been
# measuring all along.
IN_USE: Dict[str, int] = {}
LEASE_STARTED: Dict[str, float] = {}

# Config
MEM_LIMIT = CONFIG.get("crawler", {}).get("memory_threshold_percent", 95.0)
# The share of the container's process/thread allowance past which an idle browser
# is thrown away rather than handed out again.
#
# Memory is not the resource that runs out first. Chromium leaves renderer
# processes behind after a crawl, and closing the browser is the only thing that
# reclaims them: after 120 pages the container sat at 798 of its 1000 tasks with
# nothing running, holding 28 chrome processes and 725 threads, while memory was at
# 28%. The next crawl then hits the ceiling mid-run, `clone()` fails, the Playwright
# driver's pipe closes, and every page still queued dies at once. 435 of 500 pages
# of feelporto.com were lost that way on 2026-09-01.
#
# So this is not a brake like the memory threshold — there is nothing to wait for,
# because the tasks are not going to be given back. It is a recycle point.
try:
    from crawl4ai.resource_limits import default_task_recycle_percent
    TASK_RECYCLE_LIMIT = CONFIG.get("crawler", {}).get("pool", {}).get(
        "task_recycle_percent"
    ) or default_task_recycle_percent()
except Exception:
    TASK_RECYCLE_LIMIT = CONFIG.get("crawler", {}).get("pool", {}).get("task_recycle_percent", 60.0)
BASE_IDLE_TTL = CONFIG.get("crawler", {}).get("pool", {}).get("idle_ttl_sec", 300)
# A lease that is never given back would pin a browser for the life of the
# process — an async generator that nobody consumes or closes is enough to do
# it. Past this age the janitor stops believing the lease and reclaims the
# browser, so a leak costs one long-lived browser rather than the pool.
MAX_LEASE_SEC = CONFIG.get("crawler", {}).get("pool", {}).get("max_lease_sec", 3600)
DEFAULT_CONFIG_SIG = None  # Cached sig for default config

def _sig(cfg: BrowserConfig) -> str:
    """Generate config signature."""
    payload = json.dumps(cfg.to_dict(), sort_keys=True, separators=(",",":"))
    return hashlib.sha1(payload.encode()).hexdigest()

def _is_default_config(sig: str) -> bool:
    """Check if config matches default."""
    return sig == DEFAULT_CONFIG_SIG

def _lease_acquire(sig: str) -> None:
    """Mark a browser as working. Called as a crawl starts."""
    if IN_USE.get(sig, 0) == 0:
        LEASE_STARTED[sig] = time.time()
    IN_USE[sig] = IN_USE.get(sig, 0) + 1
    LAST_USED[sig] = time.time()

def _lease_release(sig: str) -> None:
    """Mark one crawl as finished, and restart the idle clock from now."""
    remaining = IN_USE.get(sig, 0) - 1
    if remaining > 0:
        IN_USE[sig] = remaining
    else:
        IN_USE.pop(sig, None)
        LEASE_STARTED.pop(sig, None)
    # Idleness is measured from when the work stopped, not from when the browser
    # was handed out.
    LAST_USED[sig] = time.time()

def is_busy(sig: str, now: Optional[float] = None) -> bool:
    """Whether a crawl is running on this browser, and the lease is still credible."""
    if IN_USE.get(sig, 0) <= 0:
        return False
    started = LEASE_STARTED.get(sig)
    if started is None:
        return True
    if (now or time.time()) - started <= MAX_LEASE_SEC:
        return True
    logger.warning(
        f"⚠️  Lease on browser (sig={sig[:8]}) held for over {MAX_LEASE_SEC}s "
        f"with {IN_USE.get(sig, 0)} crawl(s) outstanding — treating it as idle"
    )
    return False

# Reaching a pooled crawler's browser is best-effort: `crawler_strategy` and
# `browser_manager` are crawl4ai internals, and a fork that moves them must not
# make every request rebuild its browser. `_UNREACHABLE` is how "we could not
# look" is kept apart from "we looked, and it is gone".
_UNREACHABLE = object()


def _browser_of(crawler: Optional[AsyncWebCrawler]):
    """The Playwright browser behind a pooled crawler, or why it cannot be seen."""
    manager = getattr(getattr(crawler, "crawler_strategy", None), "browser_manager", None)
    if manager is None:
        return _UNREACHABLE
    # BrowserManager.close() sets this back to None, so None means closed, not unknown.
    return getattr(manager, "browser", None)


def is_alive(crawler: Optional[AsyncWebCrawler]) -> bool:
    """
    Whether a pooled browser can still be crawled with.

    Chromium dies — it is a separate process and the usual way out is the kernel
    reclaiming memory. Nothing in the pool noticed. The dead handle stayed in
    COLD_POOL and every later crawl, of every site, failed in milliseconds with
    "Browser.new_context: Target page, context or browser has been closed".

    On 2026-08-31 that took the whole platform's crawling down: feelporto.com drove
    the container from 10% memory to 99.9% in two minutes, Chromium was killed, and
    afterwards a plain crawl of example.com returned HTTP 500 with that error. The
    janitor could not clear it either — it only closes browsers it believes are
    IDLE, and a corpse looks exactly as idle as a healthy browser. Restarting the
    service was the only way out.

    Deliberately optimistic. A browser is only called dead when we can actually see
    that it is: reached it and found it closed, or asked and it said disconnected.
    Anything we cannot inspect is left alone, because wrongly discarding a healthy
    browser costs a relaunch on every single request.
    """
    if crawler is None:
        return False

    browser = _browser_of(crawler)
    if browser is _UNREACHABLE:
        return True
    if browser is None:
        return False

    try:
        return bool(browser.is_connected())
    except Exception:
        # Playwright raising while being asked the simplest question it has is not a
        # browser worth keeping.
        return False


def _forget(sig: str) -> None:
    """Drop every trace of a browser signature from the pool's bookkeeping."""
    LAST_USED.pop(sig, None)
    USAGE_COUNT.pop(sig, None)
    IN_USE.pop(sig, None)
    LEASE_STARTED.pop(sig, None)


async def _discard_dead(
    sig: str,
    crawler: Optional[AsyncWebCrawler],
    tier: Optional[Dict[str, AsyncWebCrawler]],
    tier_name: str,
) -> None:
    """
    Throw away a browser that has died, so the next caller builds a fresh one.

    Done whether or not a lease says a crawl is running on it: those crawls are
    already failing page by page, and keeping the corpse to protect them only
    spreads the failure to everybody else.

    `tier` is None for the default browser, which lives in a global rather than in
    one of the pool dicts — the caller clears that global.
    """
    if tier is not None:
        tier.pop(sig, None)
    _forget(sig)
    logger.warning(f"⚠️  Discarding dead {tier_name} browser (sig={sig[:8]}) — rebuilding on next use")
    if crawler is not None:
        with suppress(Exception):
            await crawler.close()
    try:
        from monitor import get_monitor
        await get_monitor().track_janitor_event("discard_dead", sig, {"tier": tier_name})
    except:
        pass


async def _recycle_if_starving_container(sig: str, tier: Optional[Dict[str, AsyncWebCrawler]], tier_name: str) -> bool:
    """
    Throw away an idle browser that is holding too much of the container's task
    allowance, so the next crawl starts with room to run.

    Returns True if the browser was recycled and the caller must build a fresh one.

    ⚠️ Only ever done while the browser is IDLE. Closing one mid-crawl is the exact
    failure this is trying to prevent — every page still queued behind it dies —
    so a busy browser is left alone however many tasks it is holding. A crawl that
    is already running is better finished than killed.
    """
    if is_busy(sig):
        return False

    task_pct = get_container_task_percent()
    if task_pct < TASK_RECYCLE_LIMIT:
        return False

    crawler = tier.pop(sig, None) if tier is not None else None
    _forget(sig)
    logger.warning(
        f"♻️  Recycling idle {tier_name} browser (sig={sig[:8]}) — container at "
        f"{task_pct:.0f}% of its process limit, and Chromium only gives those back when it exits"
    )
    if crawler is not None:
        with suppress(Exception):
            await crawler.close()
    try:
        from monitor import get_monitor
        await get_monitor().track_janitor_event("recycle_tasks", sig, {"tier": tier_name, "task_percent": round(task_pct, 1)})
    except:
        pass
    return True


def _track_usage(crawler: AsyncWebCrawler, sig: str) -> AsyncWebCrawler:
    """
    Hold a lease on the browser for as long as a crawl is actually running on it.

    Wrapped here rather than at each call site because there are nine of them and
    every one of them would have to remember; a browser that outlives its crawl
    is the whole failure this guards against. `arun` covers deep crawls, which
    run the entire traversal inside one call. `arun_many` returns either a list
    (work already done) or an async generator (work happens as it is consumed),
    so the streaming case holds its lease until the stream ends or is closed.
    """
    if getattr(crawler, "_pool_usage_tracked", False):
        return crawler

    original_arun = crawler.arun
    original_arun_many = crawler.arun_many

    async def arun(*args, **kwargs):
        _lease_acquire(sig)
        try:
            return await original_arun(*args, **kwargs)
        finally:
            _lease_release(sig)

    async def guarded_stream(stream):
        try:
            async for item in stream:
                LAST_USED[sig] = time.time()
                yield item
        finally:
            _lease_release(sig)

    async def arun_many(*args, **kwargs):
        _lease_acquire(sig)
        released = False
        try:
            result = await original_arun_many(*args, **kwargs)
        except BaseException:
            _lease_release(sig)
            raise
        try:
            if hasattr(result, "__aiter__"):
                return guarded_stream(result)
            released = True
            _lease_release(sig)
            return result
        except BaseException:
            if not released:
                _lease_release(sig)
            raise

    crawler.arun = arun
    crawler.arun_many = arun_many
    crawler._pool_usage_tracked = True
    return crawler

async def get_crawler(cfg: BrowserConfig) -> AsyncWebCrawler:
    """Get crawler from pool with tiered strategy."""
    global PERMANENT
    sig = _sig(cfg)
    async with LOCK:
        # Default config uses the shared default browser. It starts lazily on
        # first use (not at boot) so an idle service generates no background
        # traffic and can be put to sleep (Railway serverless).
        if _is_default_config(sig):
            if PERMANENT is not None and not is_alive(PERMANENT):
                await _discard_dead(sig, PERMANENT, None, "default")
                PERMANENT = None
            if PERMANENT is not None and not is_busy(sig) and get_container_task_percent() >= TASK_RECYCLE_LIMIT:
                task_pct = get_container_task_percent()
                logger.warning(
                    f"♻️  Recycling idle default browser — container at {task_pct:.0f}% of its "
                    f"process limit, and Chromium only gives those back when it exits"
                )
                with suppress(Exception):
                    await PERMANENT.close()
                PERMANENT = None
                _forget(sig)
            if PERMANENT is None:
                logger.info("🔥 Starting default browser (lazy)")
                PERMANENT = AsyncWebCrawler(config=DEFAULT_BROWSER_CONFIG or cfg, thread_safe=False)
                await PERMANENT.start()
            LAST_USED[sig] = time.time()
            USAGE_COUNT[sig] = USAGE_COUNT.get(sig, 0) + 1
            logger.info("🔥 Using permanent browser")
            return _track_usage(PERMANENT, sig)

        # Check hot pool
        if sig in HOT_POOL and not is_alive(HOT_POOL[sig]):
            await _discard_dead(sig, HOT_POOL[sig], HOT_POOL, "hot pool")

        if sig in HOT_POOL:
            await _recycle_if_starving_container(sig, HOT_POOL, "hot pool")

        if sig in HOT_POOL:
            LAST_USED[sig] = time.time()
            USAGE_COUNT[sig] = USAGE_COUNT.get(sig, 0) + 1
            logger.info(f"♨️  Using hot pool browser (sig={sig[:8]})")
            return _track_usage(HOT_POOL[sig], sig)

        # Check cold pool (promote to hot if used 3+ times)
        if sig in COLD_POOL and not is_alive(COLD_POOL[sig]):
            await _discard_dead(sig, COLD_POOL[sig], COLD_POOL, "cold pool")

        if sig in COLD_POOL:
            await _recycle_if_starving_container(sig, COLD_POOL, "cold pool")

        if sig in COLD_POOL:
            LAST_USED[sig] = time.time()
            USAGE_COUNT[sig] = USAGE_COUNT.get(sig, 0) + 1

            if USAGE_COUNT[sig] >= 3:
                logger.info(f"⬆️  Promoting to hot pool (sig={sig[:8]}, count={USAGE_COUNT[sig]})")
                HOT_POOL[sig] = COLD_POOL.pop(sig)

                # Track promotion in monitor
                try:
                    from monitor import get_monitor
                    await get_monitor().track_janitor_event("promote", sig, {"count": USAGE_COUNT[sig]})
                except:
                    pass

                return _track_usage(HOT_POOL[sig], sig)

            logger.info(f"❄️  Using cold pool browser (sig={sig[:8]})")
            return _track_usage(COLD_POOL[sig], sig)

        # Memory check before creating new
        mem_pct = get_container_memory_percent()
        if mem_pct >= MEM_LIMIT:
            logger.error(f"💥 Memory pressure: {mem_pct:.1f}% >= {MEM_LIMIT}%")
            raise MemoryError(f"Memory at {mem_pct:.1f}%, refusing new browser")

        # Create new in cold pool
        logger.info(f"🆕 Creating new browser in cold pool (sig={sig[:8]}, mem={mem_pct:.1f}%)")
        crawler = AsyncWebCrawler(config=cfg, thread_safe=False)
        await crawler.start()
        COLD_POOL[sig] = crawler
        LAST_USED[sig] = time.time()
        USAGE_COUNT[sig] = 1
        return _track_usage(crawler, sig)

async def init_permanent(cfg: BrowserConfig):
    """Register the default browser config. The browser itself starts lazily on
    first use (see get_crawler) and is closed by the janitor when idle, so a
    quiet service can go to sleep (Railway serverless)."""
    global DEFAULT_CONFIG_SIG, DEFAULT_BROWSER_CONFIG
    async with LOCK:
        DEFAULT_CONFIG_SIG = _sig(cfg)
        DEFAULT_BROWSER_CONFIG = cfg
        logger.info("🔥 Registered default browser config (browser starts on first use)")

async def close_permanent():
    """Close the default browser (if running). It will be recreated lazily on
    next use."""
    global PERMANENT
    async with LOCK:
        if PERMANENT is None:
            return
        with suppress(Exception):
            await PERMANENT.close()
        PERMANENT = None
        if DEFAULT_CONFIG_SIG:
            LAST_USED.pop(DEFAULT_CONFIG_SIG, None)
            USAGE_COUNT.pop(DEFAULT_CONFIG_SIG, None)

async def close_all():
    """Close all browsers."""
    async with LOCK:
        tasks = []
        if PERMANENT:
            tasks.append(PERMANENT.close())
        tasks.extend([c.close() for c in HOT_POOL.values()])
        tasks.extend([c.close() for c in COLD_POOL.values()])
        await asyncio.gather(*tasks, return_exceptions=True)
        HOT_POOL.clear()
        COLD_POOL.clear()
        LAST_USED.clear()
        USAGE_COUNT.clear()

async def janitor():
    """Adaptive cleanup based on memory pressure."""
    global PERMANENT
    while True:
        mem_pct = get_container_memory_percent()

        # Adaptive intervals and TTLs
        if mem_pct > 80:
            interval, cold_ttl, hot_ttl = 10, 30, 120
        elif mem_pct > 60:
            interval, cold_ttl, hot_ttl = 30, 60, 300
        else:
            interval, cold_ttl, hot_ttl = 60, BASE_IDLE_TTL, BASE_IDLE_TTL * 2

        await asyncio.sleep(interval)

        now = time.time()
        async with LOCK:
            # Dead browsers go first, before any question of idleness. A corpse
            # looks exactly as idle as a healthy browser, so the TTL checks below
            # would keep one for its full 300s and hand it to everyone who asked
            # in the meantime. This is also the only thing that clears one while
            # nobody is crawling, which is when it does the least harm.
            for tier, tier_name in ((COLD_POOL, "cold pool"), (HOT_POOL, "hot pool")):
                for sig in list(tier.keys()):
                    if not is_alive(tier[sig]):
                        await _discard_dead(sig, tier[sig], tier, tier_name)

            if PERMANENT is not None and not is_alive(PERMANENT):
                await _discard_dead(DEFAULT_CONFIG_SIG or "default", PERMANENT, None, "default")
                PERMANENT = None

            # Then browsers that are alive but hoarding the container's process
            # allowance. Chromium hands those back only when it exits, so waiting
            # for the idle TTL just means the next crawl starts with no room. Done
            # here as well as in get_crawler so a container left near the ceiling
            # recovers on its own rather than on the next request.
            task_pct = get_container_task_percent()
            if task_pct >= TASK_RECYCLE_LIMIT:
                for tier, tier_name in ((COLD_POOL, "cold pool"), (HOT_POOL, "hot pool")):
                    for sig in list(tier.keys()):
                        await _recycle_if_starving_container(sig, tier, tier_name)
                if PERMANENT is not None and DEFAULT_CONFIG_SIG and not is_busy(DEFAULT_CONFIG_SIG, now):
                    logger.warning(
                        f"♻️  Recycling idle default browser — container at {task_pct:.0f}% of its process limit"
                    )
                    with suppress(Exception):
                        await PERMANENT.close()
                    PERMANENT = None
                    _forget(DEFAULT_CONFIG_SIG)

            # Clean cold pool. A browser with a crawl running on it is never
            # idle, whatever its timestamp says — closing one mid-crawl kills
            # every page still queued behind it.
            for sig in list(COLD_POOL.keys()):
                if is_busy(sig, now):
                    continue
                if now - LAST_USED.get(sig, now) > cold_ttl:
                    idle_time = now - LAST_USED[sig]
                    logger.info(f"🧹 Closing cold browser (sig={sig[:8]}, idle={idle_time:.0f}s)")
                    with suppress(Exception):
                        await COLD_POOL[sig].close()
                    COLD_POOL.pop(sig, None)
                    LAST_USED.pop(sig, None)
                    USAGE_COUNT.pop(sig, None)

                    # Track in monitor
                    try:
                        from monitor import get_monitor
                        await get_monitor().track_janitor_event("close_cold", sig, {"idle_seconds": int(idle_time), "ttl": cold_ttl})
                    except:
                        pass

            # Clean hot pool (more conservative)
            for sig in list(HOT_POOL.keys()):
                if is_busy(sig, now):
                    continue
                if now - LAST_USED.get(sig, now) > hot_ttl:
                    idle_time = now - LAST_USED[sig]
                    logger.info(f"🧹 Closing hot browser (sig={sig[:8]}, idle={idle_time:.0f}s)")
                    with suppress(Exception):
                        await HOT_POOL[sig].close()
                    HOT_POOL.pop(sig, None)
                    LAST_USED.pop(sig, None)
                    USAGE_COUNT.pop(sig, None)

                    # Track in monitor
                    try:
                        from monitor import get_monitor
                        await get_monitor().track_janitor_event("close_hot", sig, {"idle_seconds": int(idle_time), "ttl": hot_ttl})
                    except:
                        pass

            # Close the default browser when idle (most conservative TTL) so a
            # quiet service generates no traffic and can sleep (Railway serverless)
            if PERMANENT and DEFAULT_CONFIG_SIG and not is_busy(DEFAULT_CONFIG_SIG, now):
                idle_time = now - LAST_USED.get(DEFAULT_CONFIG_SIG, now)
                if idle_time > hot_ttl * 2:
                    logger.info(f"🧹 Closing idle default browser (idle={idle_time:.0f}s, ttl={hot_ttl * 2})")
                    with suppress(Exception):
                        await PERMANENT.close()
                    PERMANENT = None
                    LAST_USED.pop(DEFAULT_CONFIG_SIG, None)
                    USAGE_COUNT.pop(DEFAULT_CONFIG_SIG, None)

                    # Track in monitor
                    try:
                        from monitor import get_monitor
                        await get_monitor().track_janitor_event("close_default", DEFAULT_CONFIG_SIG, {"idle_seconds": int(idle_time), "ttl": hot_ttl * 2})
                    except:
                        pass

            # Log pool stats
            if mem_pct > 60:
                logger.info(f"📊 Pool: hot={len(HOT_POOL)}, cold={len(COLD_POOL)}, mem={mem_pct:.1f}%")
