"""Relay audit: does the TA relay hold what we recorded as published for an observer?

Exact: downloads every d tag the observer's signer has on the relay and diffs it
with `last_published`. Paging by `until` stalls — the publisher stamps thousands of
TAs with the same second — so the time range is split by NIP-45 COUNT until each
window fits one download. A few seconds and ~75 MB for a 150k-TA observer.
It checks presence only, not that a present TA carries the current values.
"""

import asyncio
import json
import time
from dataclasses import dataclass

import websockets
from fastapi import HTTPException, status
from nostr_sdk import Keys
from sqlalchemy.ext.asyncio import AsyncSession as AsyncDBSession

from app.core.config import settings
from app.repos.brainstorm_nsec import (
    get_last_published_pubkeys_by_pubkey_on_db,
    select_brainstorm_nsec_by_pubkey_on_db,
)

TA_KIND = 30382
RELAY_TIMEOUT_SECONDS = 120
WINDOW_EVENTS = 20_000  # ~10 MB per download, well under strfry's 32 MB slow-client cap
MAX_SECOND_EVENTS = 60_000  # one second can't be split; refuse rather than risk the cap
FUTURE_SLACK_SECONDS = 1_000  # relays accept events up to 15 min ahead


@dataclass(frozen=True)
class RelayAudit:
    observer: str
    signer: str
    published: int
    on_relay: int
    missing: int  # published observees with no TA on the relay
    orphans: int  # observees with a TA on the relay that we don't record as published

    @property
    def needs_resync(self) -> bool:
        return self.missing > 0


class _Relay:
    def __init__(self, ws, signer: str):
        self.ws = ws
        self.base = {"kinds": [TA_KIND], "authors": [signer]}

    async def count(self, since: int, until: int) -> int:
        await self.ws.send(
            json.dumps(["COUNT", "c", {**self.base, "since": since, "until": until}])
        )
        while True:
            frame = json.loads(await self.ws.recv())
            if frame[0] == "COUNT" and frame[1] == "c":
                return int(frame[2]["count"])

    async def d_tags(self, since: int, until: int, limit: int, into: set[str]) -> None:
        flt = {**self.base, "since": since, "until": until, "limit": limit}
        await self.ws.send(json.dumps(["REQ", "w", flt]))
        while True:
            frame = json.loads(await self.ws.recv())
            if frame[1] != "w":
                continue
            if frame[0] == "EVENT":
                for tag in frame[2]["tags"]:
                    if len(tag) > 1 and tag[0] == "d":
                        into.add(tag[1])
                        break
            elif frame[0] in ("EOSE", "CLOSED"):
                await self.ws.send(json.dumps(["CLOSE", "w"]))
                return


async def _walk(relay: _Relay, since: int, until: int, n: int, into: set[str]) -> None:
    if n == 0:
        return
    if n <= WINDOW_EVENTS or since == until:
        if n > MAX_SECOND_EVENTS:
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail=f"{n} TAs share one second; too dense to check without risking the relay",
            )
        await relay.d_tags(since, until, n, into)
        return
    mid = (since + until) // 2
    await _walk(relay, since, mid, await relay.count(since, mid), into)
    await _walk(relay, mid + 1, until, await relay.count(mid + 1, until), into)


async def read_relay_d_tags(signer: str) -> tuple[int, set[str]]:
    """(the signer's TA count, the set of observees it has a TA for)."""
    async with websockets.connect(
        settings.nostr_upload_ta_events_relay, max_size=None
    ) as ws:
        relay = _Relay(ws, signer)
        until = int(time.time()) + FUTURE_SLACK_SECONDS
        total = await relay.count(0, until)
        d_tags: set[str] = set()
        await _walk(relay, 0, until, total, d_tags)
    return total, d_tags


async def audit_observer_relay(db: AsyncDBSession, observer: str) -> RelayAudit:
    nsec_row = await select_brainstorm_nsec_by_pubkey_on_db(db, observer)
    signer = Keys.parse(secret_key=nsec_row.nsec).public_key().to_hex()
    published = set(await get_last_published_pubkeys_by_pubkey_on_db(db, observer))

    try:
        on_relay, d_tags = await asyncio.wait_for(
            read_relay_d_tags(signer), RELAY_TIMEOUT_SECONDS
        )
    except (TimeoutError, OSError, websockets.WebSocketException) as exc:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=f"Could not read the TA relay: {exc.__class__.__name__}",
        )
    return RelayAudit(
        observer=observer,
        signer=signer,
        published=len(published),
        on_relay=on_relay,
        missing=len(published - d_tags),
        orphans=len(d_tags - published),
    )
