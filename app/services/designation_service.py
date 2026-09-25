"""The keys an Observer designated in their kind-10040.

A kind-10040 lists, per assertion kind, the provider pubkeys the Observer trusts
to publish Trusted Assertions for them — rows like
`["30382:rank", <provider pubkey>, <relay>]`. GrapeRank scores every such key at
a fixed 95 (see `DESIGNATED_KEY_INFLUENCE` in brainstorm_graperank_algorithm), so
the calc request carries them as `designated_pubkeys`.

Best-effort: a relay miss or error yields `[]` and the run proceeds without the
override. Only public tags are read — entries the Observer encrypted into the
content need the Observer's key, which this server does not hold.
"""
from __future__ import annotations

import asyncio
import json
import re

import websockets

from app.core.config import settings
from app.core.loggr import loggr

logger = loggr.get_logger(__name__)

DESIGNATION_KIND = 10040

# `<kind>` or `<kind>:<metric>`, e.g. "30382:rank", "30392".
_DESIGNATION_TAG_NAME = re.compile(r"^\d+(:.*)?$")
_HEX_PUBKEY = re.compile(r"^[0-9a-f]{64}$")

# In-cluster relay, one small filter: a healthy answer takes milliseconds.
_FETCH_TIMEOUT_SECONDS = 5.0


def parse_designated_pubkeys(tags: list[list[str]], observer: str) -> list[str]:
    """Provider pubkeys named by a kind-10040's designation rows, in order,
    de-duplicated, never including the Observer."""
    seen: set[str] = set()
    out: list[str] = []
    for tag in tags:
        if (
            len(tag) < 2
            or not isinstance(tag[0], str)
            or not isinstance(tag[1], str)
            or not _DESIGNATION_TAG_NAME.match(tag[0])
        ):
            continue
        pubkey = tag[1].lower()
        if not _HEX_PUBKEY.match(pubkey) or pubkey == observer or pubkey in seen:
            continue
        seen.add(pubkey)
        out.append(pubkey)
    return out


async def _fetch_latest_designation(relay: str, observer: str) -> dict | None:
    """The latest kind-10040 `relay` holds for `observer`, or None. Raises on
    transport errors and timeouts.

    A plain REQ-until-EOSE over one websocket, like `report_relay_service`: the
    nostr_sdk `Client` returns before its socket is open, so an immediate
    `fetch_events` silently comes back empty.
    """
    sub_id = f"designation-{observer[:8]}"
    req = json.dumps(
        [
            "REQ",
            sub_id,
            {"kinds": [DESIGNATION_KIND], "authors": [observer], "limit": 1},
        ]
    )
    latest: dict | None = None
    async with asyncio.timeout(_FETCH_TIMEOUT_SECONDS):
        async with websockets.connect(relay, open_timeout=_FETCH_TIMEOUT_SECONDS) as ws:
            await ws.send(req)
            while True:
                msg = json.loads(await ws.recv())
                if not isinstance(msg, list) or len(msg) < 2 or msg[1] != sub_id:
                    continue
                if msg[0] == "EVENT" and len(msg) >= 3:
                    event = msg[2]
                    if latest is None or event.get("created_at", 0) > latest.get(
                        "created_at", 0
                    ):
                        latest = event
                elif msg[0] in ("EOSE", "CLOSED"):
                    break
            try:
                await ws.send(json.dumps(["CLOSE", sub_id]))
            except Exception:
                pass
    return latest


async def fetch_designated_pubkeys(observer: str) -> list[str]:
    """The pubkeys in `observer`'s latest kind-10040, or `[]` when there is none
    or it can't be read.

    Reads our own relay (neofry), which neofry's `designations` router stream
    fills (brainstorm-k8s chart). No upstream fallback: the transferer's source
    relay holds no 10040s, and most Observers have none, so a fallback would add
    a slow external round-trip to nearly every queued run.
    """
    relay = settings.nostr_transfer_to_relay
    try:
        event = await _fetch_latest_designation(relay, observer)
    except Exception as e:
        logger.warning(f"kind-10040 lookup on {relay} failed for {observer}: {e!r}")
        return []
    if event is None:
        return []
    tags = [t for t in event.get("tags") or [] if isinstance(t, list)]
    return parse_designated_pubkeys(tags, observer)
