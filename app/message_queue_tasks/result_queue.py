"""Reliable consumption of the graperank result queues.

The worker stores each result once under `result_key` and queues a small
envelope on every result queue; `<result_key>:pending` names the queues that
still need it. Each consumer moves its message into `<queue>:processing` while
handling it, so a consumer killed mid-handler gets the message back on restart.
The payload is deleted when the last named queue releases it.

Assumes one consumer per queue: recovery requeues everything in `:processing`.
"""

from collections.abc import Awaitable, Callable

import orjson

from app.core.loggr import loggr

logger = loggr.get_logger(__name__)

MAX_ATTEMPTS = 3
ATTEMPTS_TTL_SECONDS = 7 * 24 * 60 * 60
# One GET would make Redis buffer the whole payload per reader; three readers at
# once tripled the peak. Chunks keep each reply buffer small.
READ_CHUNK_BYTES = 8 * 1024 * 1024

Handler = Callable[[dict], Awaitable[None]]
OnLost = Callable[[dict, str], Awaitable[None]]


def processing_list(queue: str) -> str:
    return f"{queue}:processing"


def _attempts_key(queue: str, message: dict) -> str:
    return f"{queue}:attempts:{message.get('result_key') or message['private_id']}"


async def read_payload(redis_client, key: str) -> bytearray | None:
    size = await redis_client.strlen(key)
    if not size:
        return None
    buf = bytearray()
    while len(buf) < size:
        # NEVER_DECODE: a chunk boundary can split a multi-byte character
        chunk = await redis_client.execute_command(
            "GETRANGE",
            key,
            len(buf),
            len(buf) + READ_CHUNK_BYTES - 1,
            NEVER_DECODE=True,
        )
        if not chunk:
            return None  # expired mid-read
        buf.extend(chunk)
    return buf


async def recover_in_flight(redis_client, queue: str) -> int:
    """Requeue interrupted messages at the head, ahead of newer runs."""
    recovered = 0
    while await redis_client.lmove(processing_list(queue), queue, "RIGHT", "LEFT"):
        recovered += 1
    if recovered:
        logger.warning(f"{queue}: requeued {recovered} interrupted message(s)")
    return recovered


async def handle_one(
    redis_client, queue: str, raw: str, handler: Handler, on_lost: OnLost
) -> None:
    message = orjson.loads(raw)
    key = message.get("result_key")
    attempts_key = _attempts_key(queue, message)
    try:
        attempts = await redis_client.incr(attempts_key)
        await redis_client.expire(attempts_key, ATTEMPTS_TTL_SECONDS)
        if attempts > MAX_ATTEMPTS:
            reason = f"gave up after {MAX_ATTEMPTS} interrupted attempts"
            logger.error(f"{queue}: run={message['private_id']} {reason}")
            await on_lost(message, reason)
        elif key is None:
            # inline payload: failure results, pre-cutover worker
            await handler(message)
        else:
            blob = await read_payload(redis_client, key)
            if blob is None:
                reason = f"result payload {key} missing (evicted or expired)"
                logger.error(f"{queue}: run={message['private_id']} {reason}")
                await on_lost(message, reason)
            else:
                message["result"] = orjson.loads(blob)["result"]
                del blob
                await handler(message)
    except Exception as e:
        logger.error(e)  # no retry: a handler exception would fail the same way again

    # Ack before release: once acked the message is never redelivered.
    await redis_client.lrem(processing_list(queue), 1, raw)
    await redis_client.delete(attempts_key)
    if key is not None:
        pending = f"{key}:pending"
        await redis_client.srem(pending, queue)
        if await redis_client.scard(pending) == 0:
            await redis_client.delete(key, pending)


async def consume_result_queue(
    redis_client, queue: str, handler: Handler, on_lost: OnLost
) -> None:
    await recover_in_flight(redis_client, queue)
    while True:
        raw = await redis_client.blmove(queue, processing_list(queue), 30)
        if raw is not None:
            await handle_one(redis_client, queue, raw, handler, on_lost)
