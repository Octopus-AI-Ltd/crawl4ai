"""Task records in Redis must always expire (OCTOPUS ADDITION).

Runs without a server or a browser — only fakeredis:
    pip install fakeredis fastapi dnspython pyyaml pytest
    pytest deploy/docker/tests/test_task_expiry.py
"""
import asyncio
import os
import sys

import fakeredis

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import utils  # noqa: E402
from utils import finish_task, stamp_heartbeat, start_task  # noqa: E402

KEY = "task:crawl_deadbeef"


def run(coro):
    return asyncio.run(coro)


def fresh():
    return fakeredis.FakeAsyncRedis()


def test_a_new_task_gets_the_long_backstop_expiry():
    async def go():
        r = fresh()
        await start_task(r, KEY, {"status": "processing", "created_at": "2026-09-10T00:00:00"})
        ttl = await r.ttl(KEY)
        assert utils.TASK_PENDING_TTL_SECONDS - 5 < ttl <= utils.TASK_PENDING_TTL_SECONDS
        assert (await r.hget(KEY, "status")) == b"processing"
    run(go())


def test_finishing_a_task_shortens_the_expiry_to_the_result_ttl():
    async def go():
        r = fresh()
        await start_task(r, KEY, {"status": "processing"})
        await finish_task(r, KEY, {"status": "completed", "result": "x" * 1000})
        ttl = await r.ttl(KEY)
        assert utils.TASK_RESULT_TTL_SECONDS - 5 < ttl <= utils.TASK_RESULT_TTL_SECONDS
        assert (await r.hget(KEY, "status")) == b"completed"
    run(go())


def test_finish_on_a_record_that_had_no_expiry_still_sets_one():
    # e.g. a task created by the previous release, before this change
    async def go():
        r = fresh()
        await r.hset(KEY, mapping={"status": "processing"})
        assert await r.ttl(KEY) == -1
        await finish_task(r, KEY, {"status": "failed", "error": "boom"})
        assert await r.ttl(KEY) > 0
    run(go())


def test_a_late_heartbeat_keeps_the_result_ttl():
    async def go():
        r = fresh()
        await start_task(r, KEY, {"status": "processing"})
        await finish_task(r, KEY, {"status": "completed"})
        await stamp_heartbeat(r, KEY, "2026-09-10T00:00:10")
        assert await r.ttl(KEY) <= utils.TASK_RESULT_TTL_SECONDS
    run(go())


def test_a_heartbeat_after_the_record_was_deleted_cannot_leave_an_immortal_key():
    async def go():
        r = fresh()
        await start_task(r, KEY, {"status": "completed"})
        await r.delete(KEY)  # what handle_task_status does to an old finished task
        await stamp_heartbeat(r, KEY, "2026-09-10T00:00:10")
        assert await r.ttl(KEY) > 0
    run(go())


def test_zero_ttl_means_no_expiry(monkeypatch):
    async def go():
        r = fresh()
        await start_task(r, KEY, {"status": "processing"})
        await finish_task(r, KEY, {"status": "completed"})
        assert await r.ttl(KEY) == -1
    monkeypatch.setattr(utils, "TASK_RESULT_TTL_SECONDS", 0)
    monkeypatch.setattr(utils, "TASK_PENDING_TTL_SECONDS", 0)
    run(go())
