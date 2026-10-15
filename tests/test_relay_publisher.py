"""The raw-websocket publisher against an in-process fake relay: every event ends
up OK-acked or reported unacked — none silently dropped."""

import asyncio
import json
from dataclasses import replace

from websockets.asyncio.server import serve

from app.message_queue_tasks.relay_publisher import PublishConfig, publish_events
from app.message_queue_tasks.ta_signing import SignedEvent

FAST = PublishConfig(
    connections=2,
    max_in_flight=20,
    max_outstanding=100,
    ack_timeout_s=0.3,
    max_event_attempts=3,
    max_connect_attempts=3,
    retry_base_delay_s=0.01,
    open_timeout_s=1.0,
)


def _events(n: int) -> list[SignedEvent]:
    return [
        SignedEvent(f"{i:064x}", json.dumps({"id": f"{i:064x}", "kind": 30382}))
        for i in range(n)
    ]


async def _stream(events, produced: list[int] | None = None):
    for e in events:
        if produced is not None:
            produced[0] += 1
        yield e


class FakeRelay:
    """`reply(conn, event_id, relay)` returns an OK tuple `(ok, reason)`, "close"
    to drop the connection, or None to stay silent."""

    def __init__(self, reply):
        self.reply = reply
        self.connections = 0
        self.seen: dict[str, int] = {}
        self.stored: set[str] = set()

    async def handler(self, ws):
        conn = self.connections
        self.connections += 1
        async for raw in ws:
            msg = json.loads(raw)
            event_id = msg[1]["id"]
            self.seen[event_id] = self.seen.get(event_id, 0) + 1
            answer = self.reply(conn, event_id, self)
            if answer == "close":
                await ws.close()
                return
            if answer is None:
                continue
            ok, reason = answer
            if ok:
                self.stored.add(event_id)
            await ws.send(json.dumps(["OK", event_id, ok, reason]))


def _run(reply, events, config=FAST, produced=None):
    relay = FakeRelay(reply)

    async def main():
        async with serve(relay.handler, "127.0.0.1", 0, close_timeout=0.1) as server:
            port = server.sockets[0].getsockname()[1]
            return await publish_events(
                f"ws://127.0.0.1:{port}", _stream(events, produced), config
            )

    return asyncio.run(main()), relay


def test_every_event_is_acked():
    events = _events(300)

    stats, relay = _run(lambda conn, eid, r: (True, ""), events)

    assert stats.n_acked == stats.n_events == 300
    assert stats.n_unacked == 0 and not stats.aborted
    assert relay.stored == {e.id for e in events}


def test_duplicate_ok_counts_as_delivered():
    stats, _ = _run(lambda conn, eid, r: (True, "duplicate: have it"), _events(10))

    assert stats.n_acked == 10 and stats.n_failed == 0


def test_a_dropped_connection_resends_its_unacked_events():
    events = _events(200)

    def reply(conn, eid, relay):
        if conn == 0 and len(relay.seen) >= 30:
            return "close"
        return (True, "")

    stats, relay = _run(reply, events, replace(FAST, connections=1))

    assert stats.n_acked == 200 and stats.n_unacked == 0
    assert stats.n_reconnects == 1
    assert relay.stored == {e.id for e in events}


def test_rate_limited_events_are_retried_until_accepted():
    events = _events(50)

    def reply(conn, eid, relay):
        if relay.seen[eid] == 1:
            return (False, "rate-limited: slow down")
        return (True, "")

    stats, relay = _run(reply, events)

    assert stats.n_acked == 50
    assert stats.n_retried == 50
    assert stats.rejected["rate-limited"] == 50
    assert all(n == 2 for n in relay.seen.values())


def test_permanently_rejected_events_are_reported_not_dropped():
    events = _events(10)

    stats, relay = _run(lambda conn, eid, r: (False, "invalid: bad signature"), events)

    assert stats.n_acked == 0
    assert stats.n_failed == stats.n_unacked == 10
    assert stats.rejected["invalid"] == 10 * FAST.max_event_attempts
    assert all(n == FAST.max_event_attempts for n in relay.seen.values())
    assert stats.failure_samples and "invalid: bad signature" in stats.failure_samples[0]
    assert not stats.aborted


def test_a_silent_connection_times_out_and_its_events_move_on():
    events = _events(40)

    def reply(conn, eid, relay):
        return None if conn == 0 else (True, "")

    # One connection, so finishing requires timing out and reconnecting.
    stats, relay = _run(reply, events, replace(FAST, connections=1))

    assert stats.n_acked == 40
    assert stats.n_reconnects == 1
    assert relay.stored == {e.id for e in events}


def test_an_unreachable_relay_aborts_with_everything_unacked():
    async def main():
        return await publish_events("ws://127.0.0.1:9", _stream(_events(5)), FAST)

    stats = asyncio.run(main())

    assert stats.aborted
    assert stats.n_acked == 0 and stats.n_unacked == stats.n_events


def test_nothing_to_publish_returns_immediately():
    stats, _ = _run(lambda conn, eid, r: (True, ""), [])

    assert stats.n_events == 0 and stats.n_unacked == 0 and not stats.aborted


def test_unresolved_events_are_capped_by_max_outstanding():
    # A relay that never answers: the producer must stop pulling (and signing)
    # events once max_outstanding are unresolved.
    config = PublishConfig(
        connections=1, max_outstanding=7, ack_timeout_s=60, open_timeout_s=1.0
    )
    produced = [0]
    relay = FakeRelay(lambda conn, eid, r: None)

    async def main():
        async with serve(relay.handler, "127.0.0.1", 0, close_timeout=0.1) as server:
            port = server.sockets[0].getsockname()[1]
            try:
                await asyncio.wait_for(
                    publish_events(
                        f"ws://127.0.0.1:{port}",
                        _stream(_events(100), produced),
                        config,
                    ),
                    timeout=0.5,
                )
            except TimeoutError:
                pass

    asyncio.run(main())

    assert produced[0] <= config.max_outstanding + 1
