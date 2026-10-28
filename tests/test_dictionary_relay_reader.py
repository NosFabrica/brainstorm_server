"""The strict reader of kind-39998 headers (ADR assistant-dictionary/0001 D3).

Run over a real local websocket, as `test_designated_pubkeys.py` runs
`fetch_latest_designation`: nostr-sdk's `Client` returns before its socket
opens, so a quick fetch through it silently finds nothing. A relay that
doesn't answer must raise, never read as one holding nothing: the Assistant
must not publish over a header it couldn't see.
"""

from __future__ import annotations

import asyncio
import json
import socket
import time

import pytest
from nostr_sdk import EventBuilder, Keys, Kind, Tag, Timestamp

from app.services import dictionary_relay_reader
from app.services.dictionary_relay_reader import fetch_newest_headers


class GuardTimeout(Exception):
    """The test's own limit ran out: the reader didn't give up by itself."""


def signed(
    keys: Keys, d: str, created_at: int, extra: list[list[str]] | None = None
) -> dict:
    tags = [Tag.parse(["d", d])] + [Tag.parse(t) for t in extra or []]
    event = (
        EventBuilder(kind=Kind(39998), content="")
        .tags(tags)
        .custom_created_at(Timestamp.from_secs(created_at))
        .sign_with_keys(keys)
    )
    return json.loads(event.as_json())


def hexkey(keys: Keys) -> str:
    return keys.public_key().to_hex()


def _read(handler, authors: list[str], ds: list[str], guard: float = 10.0):
    """Serve `handler` as a one-shot local relay and read from it. A read still
    going after `guard` seconds raises `GuardTimeout`, never the reader's own
    error, so a test can tell the reader timing out from the reader hanging."""
    import websockets

    async def run():
        server = await websockets.serve(handler, "127.0.0.1", 0)
        port = server.sockets[0].getsockname()[1]
        read = asyncio.ensure_future(
            fetch_newest_headers(f"ws://127.0.0.1:{port}", authors, ds)
        )
        try:
            done, _ = await asyncio.wait({read}, timeout=guard)
            if not done:
                read.cancel()
                raise GuardTimeout()
            return read.result()
        finally:
            server.close()
            await server.wait_closed()

    return asyncio.run(run())


def _relay_sending(*events: dict, filters: list | None = None):
    async def relay(ws):
        _, sub_id, flt = json.loads(await ws.recv())
        if filters is not None:
            filters.append(flt)
        for event in events:
            await ws.send(json.dumps(["EVENT", sub_id, event]))
        await ws.send(json.dumps(["EOSE", sub_id]))
        async for _ in ws:
            pass

    return relay


# W1 (D3): newest by created_at, whichever the relay sends first
@pytest.mark.parametrize(
    "newer_first", [True, False], ids=["newer-first", "older-first"]
)
def test_returns_the_newest_verified_header_for_each_author_and_d(newer_first):
    a, b = Keys.generate(), Keys.generate()
    a_old, a_new = signed(a, "x", 1), signed(a, "x", 2)
    b_y = signed(b, "y", 5)
    order = (a_new, a_old) if newer_first else (a_old, a_new)
    found = _read(_relay_sending(*order, b_y), [hexkey(a), hexkey(b)], ["x", "y"])
    assert found == {(hexkey(a), "x"): a_new, (hexkey(b), "y"): b_y}


# W2 (D3)
def test_asks_for_kind_39998_by_the_given_authors_and_ds():
    a = Keys.generate()
    filters: list = []
    _read(_relay_sending(filters=filters), [hexkey(a)], ["x", "lists:github:accounts"])
    [flt] = filters
    assert flt["kinds"] == [39998]
    assert set(flt["authors"]) == {hexkey(a)}
    assert set(flt["#d"]) == {"x", "lists:github:accounts"}


# W3 (D3)
def test_an_event_whose_signature_doesnt_verify_is_skipped():
    a = Keys.generate()
    good = signed(a, "x", 1)
    forged = signed(a, "x", 2)
    forged["tags"] = forged["tags"] + [["names", "Forged", "Forged"]]
    found = _read(_relay_sending(forged, good), [hexkey(a)], ["x"])
    assert found == {(hexkey(a), "x"): good}


# W4 (D3)
def test_a_relay_holding_nothing_reads_as_nothing():
    a = Keys.generate()
    assert _read(_relay_sending(), [hexkey(a)], ["x"]) == {}


# W5 (D3: strict)
def test_a_closed_request_raises():
    a = Keys.generate()

    async def relay(ws):
        _, sub_id, _ = json.loads(await ws.recv())
        await ws.send(json.dumps(["CLOSED", sub_id, "auth-required: sign in"]))
        async for _ in ws:
            pass

    with pytest.raises(Exception):
        _read(relay, [hexkey(a)], ["x"])


# W6 (D3: strict)
def test_a_relay_that_cant_be_reached_raises():
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    with pytest.raises(Exception):
        asyncio.run(
            fetch_newest_headers(
                f"ws://127.0.0.1:{port}", [hexkey(Keys.generate())], ["x"]
            )
        )


# W7 (D3: strict, the read's bound)
def test_a_relay_that_takes_the_request_and_goes_quiet_raises_within_the_bound(
    monkeypatch,
):
    monkeypatch.setattr(dictionary_relay_reader, "READ_TIMEOUT_SECONDS", 0.3)
    a = Keys.generate()

    async def relay(ws):
        await ws.recv()  # the REQ, never answered: no EVENT, no EOSE
        async for _ in ws:
            pass

    started = time.monotonic()
    with pytest.raises(Exception) as raised:
        _read(relay, [hexkey(a)], ["x"], guard=3.0)
    assert not isinstance(raised.value, GuardTimeout)
    assert time.monotonic() - started < 2.0


# W8 (D3: strict, a transport error mid-read)
def test_a_relay_that_closes_before_eose_raises_rather_than_reading_as_partial():
    a = Keys.generate()
    event = signed(a, "x", 1)

    async def relay(ws):
        _, sub_id, _ = json.loads(await ws.recv())
        await ws.send(json.dumps(["EVENT", sub_id, event]))
        await ws.close()

    with pytest.raises(Exception) as raised:
        _read(relay, [hexkey(a)], ["x"], guard=3.0)
    assert not isinstance(raised.value, GuardTimeout)
