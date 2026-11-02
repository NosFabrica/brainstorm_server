"""A handler that raises still closes its stage, so the run leaves the pipeline."""

import asyncio
from unittest.mock import AsyncMock

import orjson

from app.message_queue_tasks.result_queue import handle_one


def test_a_handler_exception_marks_the_stage_lost():
    redis = AsyncMock()
    redis.incr.return_value = 1
    on_lost = AsyncMock()

    async def handler(message):
        raise RuntimeError("boom")

    raw = orjson.dumps({"private_id": 7, "result": {"success": True}})
    asyncio.run(handle_one(redis, "q", raw, handler, on_lost))

    message, reason = on_lost.await_args.args
    assert message["private_id"] == 7
    assert "boom" in reason
    redis.lrem.assert_awaited()


def test_an_on_lost_failure_does_not_stop_the_ack():
    redis = AsyncMock()
    redis.incr.return_value = 1

    async def handler(message):
        raise RuntimeError("boom")

    raw = orjson.dumps({"private_id": 7, "result": {"success": True}})
    asyncio.run(handle_one(redis, "q", raw, handler, AsyncMock(side_effect=RuntimeError("db down"))))

    redis.lrem.assert_awaited()
