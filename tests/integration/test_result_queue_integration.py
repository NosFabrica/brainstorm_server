"""Result-queue lifecycle against a real Redis: one stored payload, three queues,
deleted when the last queue releases it, redelivered when a consumer dies."""

import asyncio
import json
import uuid

import pytest

from app.core.redis_db import get_redis_client
from app.message_queue_tasks import result_queue
from app.message_queue_tasks.result_queue import (
    MAX_ATTEMPTS,
    handle_one,
    processing_list,
    read_payload,
    recover_in_flight,
)

pytestmark = pytest.mark.integration

RESULT = {"scorecards": None, "duration_seconds": 1.0, "success": True}


def with_redis(test):
    """Run an async test body with a fresh client and a key prefix it cleans up."""

    def run():
        async def body():
            client = get_redis_client()
            prefix = f"test:rq:{uuid.uuid4().hex}"
            try:
                await test(client, prefix)
            finally:
                keys = [k async for k in client.scan_iter(f"{prefix}*", count=100_000)]
                if keys:
                    await client.delete(*keys)
                await client.aclose()

        asyncio.run(body())

    run.__name__ = (
        test.__name__
    )  # not functools.wraps: pytest would read test's args as fixtures
    return run


async def _publish(client, prefix, queues, private_id=7):
    """What the Java worker's ResultPayload.publish writes: the full MessageQueueReturnValue."""
    key = f"{prefix}:result:{private_id}:{uuid.uuid4().hex}"
    await client.set(key, json.dumps({"result": RESULT, "private_id": private_id}))
    await client.sadd(f"{key}:pending", *queues)
    envelope = json.dumps({"private_id": private_id, "result_key": key})
    for q in queues:
        await client.rpush(q, envelope)
    return key, envelope


async def _take(client, queue):
    return await client.lmove(queue, processing_list(queue), "LEFT", "RIGHT")


class Recorder:
    def __init__(self, raises=False):
        self.handled, self.lost, self.raises = [], [], raises

    async def handler(self, message):
        self.handled.append(message)
        if self.raises:
            raise RuntimeError("boom")

    async def on_lost(self, message, reason):
        self.lost.append(reason)


@with_redis
async def test_payload_deleted_only_after_every_queue_released(client, prefix):
    queues = [f"{prefix}:q{i}" for i in range(3)]
    key, _ = await _publish(client, prefix, queues)
    rec = Recorder()

    for i, q in enumerate(queues):
        await handle_one(client, q, await _take(client, q), rec.handler, rec.on_lost)
        assert rec.handled[-1]["result"] == RESULT
        assert await client.exists(key) == (0 if i == 2 else 1)

    assert await client.exists(f"{key}:pending") == 0
    for q in queues:
        assert await client.llen(processing_list(q)) == 0


@with_redis
async def test_handler_exception_still_acks_and_releases(client, prefix):
    q = f"{prefix}:q"
    key, _ = await _publish(client, prefix, [q])

    await handle_one(client, q, await _take(client, q), *_fns(Recorder(raises=True)))

    assert await client.exists(key) == 0
    assert await client.llen(processing_list(q)) == 0


@with_redis
async def test_releasing_twice_does_not_free_payload_early(client, prefix):
    a, b = f"{prefix}:a", f"{prefix}:b"
    key, envelope = await _publish(client, prefix, [a, b])
    rec = Recorder()

    await handle_one(client, a, await _take(client, a), *_fns(rec))
    await client.rpush(processing_list(a), envelope)  # redelivered duplicate
    await handle_one(client, a, envelope, *_fns(rec))

    assert await client.exists(key) == 1


@with_redis
async def test_missing_payload_fails_loudly_without_calling_handler(client, prefix):
    q = f"{prefix}:q"
    key, _ = await _publish(client, prefix, [q])
    await client.delete(key)
    rec = Recorder()

    await handle_one(client, q, await _take(client, q), *_fns(rec))

    assert rec.handled == []
    assert "missing" in rec.lost[0]


@with_redis
async def test_interrupted_message_is_requeued_ahead_of_newer_runs(client, prefix):
    q = f"{prefix}:q"
    _, first = await _publish(client, prefix, [q], private_id=1)
    await _take(client, q)  # consumer dies holding it
    _, second = await _publish(client, prefix, [q], private_id=2)

    assert await recover_in_flight(client, q) == 1
    assert await client.lrange(q, 0, -1) == [first, second]
    assert await client.llen(processing_list(q)) == 0


@with_redis
async def test_gives_up_after_repeated_interruptions(client, prefix):
    q = f"{prefix}:q"
    key, _ = await _publish(client, prefix, [q])
    raw = await _take(client, q)
    await client.set(f"{q}:attempts:{key}", MAX_ATTEMPTS)  # died MAX times
    rec = Recorder()

    await handle_one(client, q, raw, *_fns(rec))

    assert rec.handled == []
    assert "gave up" in rec.lost[0]
    assert await client.exists(key) == 0


@with_redis
async def test_inline_payload_still_processes(client, prefix):
    q = f"{prefix}:q"
    raw = json.dumps({"private_id": 9, "result": RESULT})
    await client.rpush(q, raw)
    rec = Recorder()

    await handle_one(client, q, await _take(client, q), *_fns(rec))

    assert rec.handled[0]["result"] == RESULT
    assert await client.llen(processing_list(q)) == 0
    assert await client.exists(f"{q}:attempts:9") == 0


@with_redis
async def test_chunked_read_survives_multibyte_split(client, prefix):
    key = f"{prefix}:blob"
    value = ("é" * 7 + "x") * 50  # 2-byte chars straddle 5-byte chunk boundaries
    await client.set(key, value)
    original = result_queue.READ_CHUNK_BYTES
    result_queue.READ_CHUNK_BYTES = 5
    try:
        assert (await read_payload(client, key)).decode() == value
    finally:
        result_queue.READ_CHUNK_BYTES = original
    assert await read_payload(client, f"{prefix}:absent") is None


def _fns(rec):
    return rec.handler, rec.on_lost
