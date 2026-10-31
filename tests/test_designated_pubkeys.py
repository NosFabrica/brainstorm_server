"""The Observer's kind-10040 keys ride on the calc request as
`designated_pubkeys`, which the GrapeRank worker scores at a fixed 95."""

import asyncio
import base64
import hashlib
import json
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.core.config import settings
from app.services.designation_service import parse_designated_pubkeys
from app.services.scheduler_lanes import enqueue_calc_request

OBSERVER = "a" * 64
PROVIDER = "b" * 64
OTHER_PROVIDER = "c" * 64


def test_reads_the_provider_from_each_designation_row():
    tags = [
        ["30382:rank", PROVIDER, "wss://nip85.brainstorm.world"],
        ["30382:followers", PROVIDER, "wss://nip85.brainstorm.world"],
        ["30392", OTHER_PROVIDER, "wss://relay.example"],
    ]
    assert parse_designated_pubkeys(tags, OBSERVER) == [PROVIDER, OTHER_PROVIDER]


def test_ignores_non_designation_tags_and_malformed_pubkeys():
    tags = [
        ["p", PROVIDER],
        ["30382:rank", "npub1notahexkey"],
        ["30382:rank"],
        ["30382:rank", "d" * 63],
    ]
    assert parse_designated_pubkeys(tags, OBSERVER) == []


def test_never_designates_the_observer():
    assert parse_designated_pubkeys([["30382:rank", OBSERVER]], OBSERVER) == []


def test_normalises_uppercase_hex():
    assert parse_designated_pubkeys([["30382:rank", PROVIDER.upper()]], OBSERVER) == [
        PROVIDER
    ]


def _enqueue(monkeypatch, designated):
    monkeypatch.setattr(settings, "scheduler_enabled", False)
    redis = SimpleNamespace(rpush=AsyncMock())
    monkeypatch.setattr("app.services.scheduler_lanes.redis_client", redis)
    instance = SimpleNamespace(
        model_dump=lambda mode=None: {"private_id": 7, "parameters": OBSERVER},
        model_dump_json=lambda: json.dumps({"private_id": 7, "parameters": OBSERVER}),
    )
    asyncio.run(
        enqueue_calc_request(AsyncMock(), instance, OBSERVER, "manual", designated)
    )
    return json.loads(redis.rpush.await_args.args[1])


def test_enqueue_carries_the_designated_pubkeys(monkeypatch):
    message = _enqueue(monkeypatch, [PROVIDER])
    assert message["designated_pubkeys"] == [PROVIDER]
    assert message["parameters"] == OBSERVER


def test_enqueue_without_designation_is_unchanged(monkeypatch):
    assert "designated_pubkeys" not in _enqueue(monkeypatch, [])


def test_reads_only_our_relay(monkeypatch):
    from app.services import designation_service

    asked = []

    async def fake_fetch(relay, observer):
        asked.append(relay)
        return None

    monkeypatch.setattr(designation_service, "fetch_latest_designation", fake_fetch)
    assert asyncio.run(designation_service.fetch_designated_pubkeys(OBSERVER)) == []
    assert asked == [settings.nostr_transfer_to_relay]


def test_a_relay_error_means_no_designation(monkeypatch):
    from app.services import designation_service

    async def boom(relay, observer):
        raise ConnectionError("down")

    monkeypatch.setattr(designation_service, "fetch_latest_designation", boom)
    assert asyncio.run(designation_service.fetch_designated_pubkeys(OBSERVER)) == []


def test_ignores_non_string_tag_values():
    assert (
        parse_designated_pubkeys([["30382:rank", 42], [None, PROVIDER]], OBSERVER) == []
    )


def _serve(handler):
    """Run `handler` as a one-shot local relay; returns (url, server)."""
    import websockets

    async def start():
        server = await websockets.serve(handler, "127.0.0.1", 0)
        port = server.sockets[0].getsockname()[1]
        return f"ws://127.0.0.1:{port}", server

    return start


def _fetch_from(handler):
    from app.services import designation_service

    async def run():
        url, server = await _serve(handler)()
        try:
            return await designation_service.fetch_latest_designation(url, OBSERVER)
        finally:
            server.close()
            await server.wait_closed()

    return asyncio.run(run())


def test_fetch_reads_the_newest_designation_from_a_real_socket():
    """Regression: the nostr_sdk Client returned before its socket opened, so the
    lookup silently found nothing. This runs the REQ over an actual websocket."""
    older = {"created_at": 1, "tags": [["30382:rank", OTHER_PROVIDER]]}
    newer = {"created_at": 2, "tags": [["30382:rank", PROVIDER]]}

    async def relay(ws):
        _, sub_id, flt = json.loads(await ws.recv())
        assert flt == {"kinds": [10040], "authors": [OBSERVER], "limit": 1}
        await ws.send(json.dumps(["EVENT", sub_id, older]))
        await ws.send(json.dumps(["EVENT", sub_id, newer]))
        await ws.send(json.dumps(["EOSE", sub_id]))
        async for _ in ws:
            pass

    assert _fetch_from(relay) == newer


def test_fetch_returns_none_when_the_relay_has_no_designation():
    async def relay(ws):
        _, sub_id, _ = json.loads(await ws.recv())
        await ws.send(json.dumps(["EOSE", sub_id]))
        async for _ in ws:
            pass

    assert _fetch_from(relay) is None


def test_a_closed_request_is_no_answer():
    """A refusal never reads as "no designation": the Assistant's Treasure Map
    reads its own kind 10040 strictly through this reader."""

    async def relay(ws):
        _, sub_id, _ = json.loads(await ws.recv())
        await ws.send(json.dumps(["CLOSED", sub_id, "auth-required: sign in first"]))
        async for _ in ws:
            pass

    with pytest.raises(RuntimeError, match="auth-required"):
        _fetch_from(relay)


def _ws_frame(text: str) -> bytes:
    """One unmasked text frame, as a server sends it."""
    data = text.encode()
    n = len(data)
    head = bytes([0x81, n]) if n < 126 else bytes([0x81, 126]) + n.to_bytes(2, "big")
    return head + data


async def _read_ws_frame(reader: asyncio.StreamReader) -> str:
    b1, b2 = await reader.readexactly(2)
    n = b2 & 0x7F
    if n == 126:
        n = int.from_bytes(await reader.readexactly(2), "big")
    elif n == 127:
        n = int.from_bytes(await reader.readexactly(8), "big")
    mask = await reader.readexactly(4) if b2 & 0x80 else bytes(4)
    data = await reader.readexactly(n)
    return bytes(c ^ mask[i % 4] for i, c in enumerate(data)).decode()


def test_a_relay_that_stalls_the_close_keeps_its_answer_and_costs_a_second():
    """A relay that answers and then never acknowledges the close: websockets
    would wait 10 s for it, and the answer, read in time, used to be lost to
    the deadline."""
    from app.services import designation_service

    newer = {"created_at": 2, "tags": [["30382:rank", PROVIDER]]}

    async def stalled(reader, writer):
        head = await reader.readuntil(b"\r\n\r\n")
        key = next(
            line.split(b":", 1)[1].strip()
            for line in head.split(b"\r\n")
            if line.lower().startswith(b"sec-websocket-key:")
        )
        accept = base64.b64encode(
            hashlib.sha1(key + b"258EAFA5-E914-47DA-95CA-C5AB0DC85B11").digest()
        )
        writer.write(
            b"HTTP/1.1 101 Switching Protocols\r\nUpgrade: websocket\r\n"
            b"Connection: Upgrade\r\nSec-WebSocket-Accept: " + accept + b"\r\n\r\n"
        )
        _, sub_id, _ = json.loads(await _read_ws_frame(reader))
        writer.write(_ws_frame(json.dumps(["EVENT", sub_id, newer])))
        writer.write(_ws_frame(json.dumps(["EOSE", sub_id])))
        await writer.drain()
        await asyncio.sleep(30)  # never answers the close

    async def run():
        server = await asyncio.start_server(stalled, "127.0.0.1", 0)
        port = server.sockets[0].getsockname()[1]
        try:
            return await designation_service.fetch_latest_designation(
                f"ws://127.0.0.1:{port}", OBSERVER
            )
        finally:
            server.close()

    started = time.monotonic()
    assert asyncio.run(run()) == newer
    assert time.monotonic() - started < 3
