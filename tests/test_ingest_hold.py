"""`ingest:hold`: an operator's stop for every ingest consumer, whatever naming."""
import asyncio
import json
from unittest.mock import AsyncMock

from app.message_queue_tasks import ingest_gate
from app.message_queue_tasks import message_queue_consumer as consumer
from app.services import relationship_sets

QUEUE = consumer.STRFRY_EVENTS_QUEUE_NAME


class _Redis:
    def __init__(self):
        self.kv, self.lists = {}, {}

    async def get(self, key):
        return self.kv.get(key)

    async def mget(self, *keys):
        return [self.kv.get(k) for k in keys]

    async def getset(self, key, value):
        old, self.kv[key] = self.kv.get(key), value
        return old

    async def set(self, key, value, ex=None):
        self.kv[key] = value

    async def delete(self, key):
        self.kv.pop(key, None)

    async def scan_iter(self, match):
        for key in list(self.kv):
            if key.startswith(match.rstrip("*")):
                yield key

    async def rpush(self, key, value):
        self.lists.setdefault(key, []).append(value)

    async def lpush(self, key, value):
        self.lists.setdefault(key, []).insert(0, value)

    async def blpop(self, key, timeout):
        items = self.lists.get(key)
        return (key, items.pop(0)) if items else None


def test_the_hold_stops_ingest_survives_a_claim_and_lifts_when_deleted(monkeypatch):
    processed = AsyncMock()
    monkeypatch.setattr(consumer, "process_strfry_event", processed)
    monkeypatch.setattr(consumer.neo4j_driver, "session", _NoSession)
    monkeypatch.setattr(relationship_sets.settings, "scores_in_postgres", False)
    redis = _Redis()

    async def go():
        await redis.set(ingest_gate.HOLD_KEY, "1")
        await redis.rpush(QUEUE, json.dumps({"kind": 3}))
        assert not await consumer.ingest_next_event(redis, "ingest:busy:a", 0)

        # A server start claims its naming; the hold stays and still holds.
        await ingest_gate.claim_naming(redis)
        assert await redis.get(ingest_gate.HOLD_KEY) == "1"
        assert not await ingest_gate.may_ingest(redis)
        assert not await consumer.ingest_next_event(redis, "ingest:busy:a", 0)
        assert processed.await_count == 0 and len(redis.lists[QUEUE]) == 1

        await redis.delete(ingest_gate.HOLD_KEY)
        assert await consumer.ingest_next_event(redis, "ingest:busy:a", 0)
        assert processed.await_args.args[1] == {"kind": 3}

    asyncio.run(go())


def test_the_hold_wins_over_a_naming_claimed_for_this_server(monkeypatch):
    monkeypatch.setattr(relationship_sets.settings, "scores_in_postgres", True)
    redis = _Redis()

    async def go():
        await ingest_gate.claim_naming(redis)
        assert await ingest_gate.may_ingest(redis)
        await redis.set(ingest_gate.HOLD_KEY, "1")
        assert not await ingest_gate.may_ingest(redis)

    asyncio.run(go())


def test_the_hold_is_not_a_busy_marker():
    assert not ingest_gate.HOLD_KEY.startswith(ingest_gate.BUSY_PREFIX)


class _NoSession:
    async def __aenter__(self):
        return object()

    async def __aexit__(self, *exc):
        return False
