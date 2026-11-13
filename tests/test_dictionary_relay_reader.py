"""The strict reader of kind-39998 headers (ADR assistant-dictionary/0001 D3).

Run over a real local websocket, as `test_designated_pubkeys.py` runs
`fetch_latest_designation`: nostr-sdk's `Client` returns before its socket
opens, so a quick fetch through it silently finds nothing. A relay that
doesn't answer must raise, never read as one holding nothing: the Assistant
must not publish over a header it couldn't see.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import socket
import time

import pytest
from nostr_sdk import EventBuilder, Keys, Kind, Tag, Timestamp

from app.services import dictionary_relay_reader
from app.services.dictionary_relay_reader import RelayDidNotAnswer, fetch_newest_headers
from app.utils.signed_event import Rejection, SignedEventRejected, verify_signed_event


class GuardTimeout(Exception):
    """The test's own limit ran out: the reader didn't give up by itself."""


def signed(
    keys: Keys, d: str, created_at: int, extra: list[list[str]] | None = None
) -> dict:
    return signed_tags(keys, [["d", d], *(extra or [])], created_at)


def signed_tags(keys: Keys, tags: list[list[str]], created_at: int) -> dict:
    event = (
        EventBuilder(kind=Kind(39998), content="")
        .tags([Tag.parse(t) for t in tags])
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

    with pytest.raises(RelayDidNotAnswer, match="auth-required"):
        _read(relay, [hexkey(a)], ["x"])


# W6 (D3: strict)
def test_a_relay_that_cant_be_reached_raises():
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    with pytest.raises(RelayDidNotAnswer):
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
    with pytest.raises(RelayDidNotAnswer):  # so not the guard's `GuardTimeout`
        _read(relay, [hexkey(a)], ["x"], guard=3.0)
    assert time.monotonic() - started < 2.0


# W8 (D3: strict, a transport error mid-read)
def test_a_relay_that_closes_before_eose_raises_rather_than_reading_as_partial():
    a = Keys.generate()
    event = signed(a, "x", 1)

    async def relay(ws):
        _, sub_id, _ = json.loads(await ws.recv())
        await ws.send(json.dumps(["EVENT", sub_id, event]))
        await ws.close()

    with pytest.raises(RelayDidNotAnswer):  # so not the guard's `GuardTimeout`
        _read(relay, [hexkey(a)], ["x"], guard=3.0)


# W9 (Gate B review: a malformed event is skipped, never fails the read; the
# `tags` and `d` shapes since the re-review)
@pytest.mark.parametrize(
    "field, value",
    [
        ("created_at", "soon"),
        ("tags", 5),
        ("tags", [["d", ["x"]]]),
        ("tags", [["d", {"x": 1}]]),
    ],
    ids=["created_at-not-a-number", "tags-not-a-list", "d-a-list", "d-an-object"],
)
def test_a_malformed_event_is_skipped_and_the_read_still_answers(field, value):
    a = Keys.generate()
    good = signed(a, "x", 1)
    malformed = {**signed(a, "x", 2), field: value}
    # The valid event first: comparing the malformed one against it is what
    # used to raise, before the signature check came first.
    found = _read(_relay_sending(good, malformed), [hexkey(a)], ["x"], guard=3.0)
    assert found == {(hexkey(a), "x"): good}


# W10 (Gate B re-review): a header's `d` is its first `d` tag's value, as
# nostr-sdk and the UI read it. One whose first `d` has none has no coordinate,
# so it's never read as the header at its next `d`, however new it is.
def test_a_header_whose_first_d_has_no_value_isnt_read_as_its_next_ds():
    a = Keys.generate()
    good = signed(a, "x", 1)
    no_coordinate = signed_tags(a, [["d"], ["d", "x"]], 2)
    found = _read(_relay_sending(good, no_coordinate), [hexkey(a)], ["x"])
    assert found == {(hexkey(a), "x"): good}


# W11 (after the merge with staging): each event is checked through
# `verify_signed_event`, the one way the server accepts a signed event from
# outside (#135), at the kind and author it would be filed under. One it
# rejects is skipped.
def test_each_event_is_checked_through_verify_signed_event(monkeypatch):
    a = Keys.generate()
    good, rejected = signed(a, "x", 1), signed(a, "x", 2)
    checked = []

    def verify(raw, *, kind, author=None):
        checked.append((raw["id"], kind, author))
        if raw["id"] == rejected["id"]:
            raise SignedEventRejected(Rejection.BAD_SIGNATURE)
        return verify_signed_event(raw, kind=kind, author=author)

    monkeypatch.setattr(dictionary_relay_reader, "verify_signed_event", verify)
    found = _read(_relay_sending(good, rejected), [hexkey(a)], ["x"])
    assert found == {(hexkey(a), "x"): good}
    assert checked == [
        (good["id"], 39998, hexkey(a)),
        (rejected["id"], 39998, hexkey(a)),
    ]


def _ws_frame(text: str) -> bytes:
    """One unmasked text frame, as a server sends it (as in
    `test_designated_pubkeys.py`)."""
    data = text.encode()
    n = len(data)
    head = bytes([0x81, n]) if n < 126 else bytes([0x81, 126]) + n.to_bytes(2, "big")
    return head + data


async def _read_ws_frame(reader: asyncio.StreamReader) -> str:
    """One frame from the client, unmasked."""
    _, b2 = await reader.readexactly(2)
    n = b2 & 0x7F
    if n == 126:
        n = int.from_bytes(await reader.readexactly(2), "big")
    elif n == 127:
        n = int.from_bytes(await reader.readexactly(8), "big")
    mask = await reader.readexactly(4) if b2 & 0x80 else bytes(4)
    data = await reader.readexactly(n)
    return bytes(c ^ mask[i % 4] for i, c in enumerate(data)).decode()


# W12 (Enes's review of #122): the close is bounded, and outside the deadline
def test_a_relay_that_stalls_the_close_keeps_its_answer(monkeypatch):
    """A relay that answers, then never acknowledges the close. websockets waits
    10 s for that by default, so with the close inside the read's deadline, an
    answer read in time was lost to the deadline, and the request answered 502.
    The deadline is shorter here than the close's own bound, so a close still
    inside the deadline fails this too."""
    monkeypatch.setattr(dictionary_relay_reader, "READ_TIMEOUT_SECONDS", 0.3)
    a = Keys.generate()
    event = signed(a, "x", 1)

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
        writer.write(_ws_frame(json.dumps(["EVENT", sub_id, event])))
        writer.write(_ws_frame(json.dumps(["EOSE", sub_id])))
        await writer.drain()
        await asyncio.sleep(30)  # never answers the close

    async def run():
        server = await asyncio.start_server(stalled, "127.0.0.1", 0)
        port = server.sockets[0].getsockname()[1]
        try:
            return await fetch_newest_headers(
                f"ws://127.0.0.1:{port}", [hexkey(a)], ["x"]
            )
        finally:
            server.close()

    started = time.monotonic()
    assert asyncio.run(run()) == {(hexkey(a), "x"): event}
    assert time.monotonic() - started < 3


# W13 (Enes's review of #122): a relay that sends something that isn't JSON
# didn't answer, so the request still answers 502 rather than 500
def test_a_relay_that_sends_something_that_isnt_json_didnt_answer():
    a = Keys.generate()

    async def relay(ws):
        await ws.recv()
        await ws.send("not json")
        async for _ in ws:
            pass

    with pytest.raises(RelayDidNotAnswer):
        _read(relay, [hexkey(a)], ["x"], guard=3.0)
