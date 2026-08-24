# crawler_pool.py - Smart browser pool with tiered management
import asyncio, json, hashlib, time
from contextlib import suppress
from typing import Dict, Optional
from crawl4ai import AsyncWebCrawler, BrowserConfig
from utils import load_config, get_container_memory_percent
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
            if PERMANENT is None:
                logger.info("🔥 Starting default browser (lazy)")
                PERMANENT = AsyncWebCrawler(config=DEFAULT_BROWSER_CONFIG or cfg, thread_safe=False)
                await PERMANENT.start()
            LAST_USED[sig] = time.time()
            USAGE_COUNT[sig] = USAGE_COUNT.get(sig, 0) + 1
            logger.info("🔥 Using permanent browser")
            return _track_usage(PERMANENT, sig)

        # Check hot pool
        if sig in HOT_POOL:
            LAST_USED[sig] = time.time()
            USAGE_COUNT[sig] = USAGE_COUNT.get(sig, 0) + 1
            logger.info(f"♨️  Using hot pool browser (sig={sig[:8]})")
            return _track_usage(HOT_POOL[sig], sig)

        # Check cold pool (promote to hot if used 3+ times)
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
