"""Strict reads of kind-39998 headers from the dictionary relay (ADR
assistant-dictionary/0001 D3).

The Assistant must never publish over a header it couldn't see, so a relay
that doesn't answer raises here — a transport error, a socket closed before
EOSE, a timeout or a `CLOSED` — and never reads as one holding nothing. A
plain REQ-until-EOSE over one websocket, as `designation_service`
`fetch_latest_designation` reads kind 10040: the nostr_sdk `Client` returns
before its socket is open, so an immediate `fetch_events` silently comes back
empty. Events whose signature doesn't verify are skipped.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any

import websockets
from nostr_sdk import Event  # type: ignore

from app.services.dictionary_copy_build import HEADER_KIND

# The whole read's bound (D3). Read when a read starts, so a test can shrink it.
READ_TIMEOUT_SECONDS = 5.0


def _verified(event: dict[str, Any]) -> bool:
    try:
        return Event.from_json(json.dumps(event)).verify()
    except Exception:
        return False


def _d_of(event: dict[str, Any]) -> str | None:
    for tag in event.get("tags") or []:
        if isinstance(tag, list) and len(tag) > 1 and tag[0] == "d":
            return tag[1]
    return None


def _newer(event: dict[str, Any], than: dict[str, Any] | None) -> bool:
    """NIP-01's rule for one replaceable address: the later `created_at`, then
    the lower id."""
    if than is None:
        return True
    a, b = int(event.get("created_at") or 0), int(than.get("created_at") or 0)
    return a > b or (a == b and str(event.get("id")) < str(than.get("id")))


async def fetch_newest_headers(
    relay: str, authors: list[str], ds: list[str]
) -> dict[tuple[str, str], dict[str, Any]]:
    """The newest verified kind-39998 event `relay` holds for each (author, `d`)
    among `authors` and `ds`. Raises when the relay doesn't answer in full."""
    if not authors or not ds:
        return {}
    sub_id = f"dictionary-{authors[0][:8]}"
    req = json.dumps(
        ["REQ", sub_id, {"kinds": [HEADER_KIND], "authors": authors, "#d": ds}]
    )
    wanted_authors, wanted_ds = set(authors), set(ds)
    newest: dict[tuple[str, str], dict[str, Any]] = {}
    timeout = READ_TIMEOUT_SECONDS
    async with asyncio.timeout(timeout):
        async with websockets.connect(relay, open_timeout=timeout) as ws:
            await ws.send(req)
            while True:
                msg = json.loads(await ws.recv())
                if not isinstance(msg, list) or len(msg) < 2 or msg[1] != sub_id:
                    continue
                if msg[0] == "EVENT" and len(msg) >= 3 and isinstance(msg[2], dict):
                    event = msg[2]
                    d = _d_of(event)
                    key = (str(event.get("pubkey")), d or "")
                    if (
                        event.get("kind") == HEADER_KIND
                        and key[0] in wanted_authors
                        and d in wanted_ds
                        # Verified before compared: a malformed event can't
                        # verify, and must be skipped, not fail the read.
                        and _verified(event)
                        and _newer(event, newest.get(key))
                    ):
                        newest[key] = event
                elif msg[0] == "EOSE":
                    break
                elif msg[0] == "CLOSED":
                    reason = msg[2] if len(msg) > 2 else ""
                    raise RuntimeError(f"relay closed the request: {reason}")
            try:
                await ws.send(json.dumps(["CLOSE", sub_id]))
            except Exception:
                pass
    return newest
