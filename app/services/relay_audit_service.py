"""Relay audit: does the TA relay hold every TA an observer's scores call for?

The expected set comes from Neo4j (`influence_<observer>` at or above the publish
cutoff), not `last_published`: that records what we sent, not what landed, and goes
stale after a failed run. Its drift from Neo4j is reported alongside.

Downloads every d tag the observer's signer has on the relay and diffs it with
the expected set. Paging by `until` stalls — the publisher stamps thousands of TAs
with the same second — so the time range is split by NIP-45 COUNT until each
window fits one download. A few seconds and ~75 MB for a 150k-TA observer.

A single second too dense to download safely is never fetched whole: published
observees not found elsewhere are looked up by d tag instead — exactly when few,
by a sample (`exact=False`) when many. Presence only, not current values.
"""

import asyncio
import json
import random
import time
from dataclasses import dataclass

import websockets
from fastapi import HTTPException, status
from nostr_sdk import Keys
from sqlalchemy.ext.asyncio import AsyncSession as AsyncDBSession

from app.core.config import settings
from app.neo4j_db.driver import driver as neo4j_driver
from app.repos.brainstorm_nsec import (
    get_last_published_pubkeys_by_pubkey_on_db,
    select_brainstorm_nsec_by_pubkey_on_db,
)
from app.repos.user_repo import get_ta_observees_for_observer

TA_KIND = 30382
RELAY_TIMEOUT_SECONDS = 120
WINDOW_EVENTS = 20_000  # ~10 MB per download, well under strfry's 32 MB slow-client cap
MAX_SECOND_EVENTS = 40_000  # a denser second is not downloaded (can't be split)
D_TAG_CHUNK = 500
EXACT_DENSE_CANDIDATES = 5_000  # beyond this, sample the dense seconds instead
DENSE_SAMPLE = 500
FUTURE_SLACK_SECONDS = 1_000  # relays accept events up to 15 min ahead


@dataclass(frozen=True)
class RelayAudit:
    observer: str
    signer: str
    expected: int  # observees at or above the cutoff in Neo4j
    published: int  # size of last_published
    baseline_drift: int  # observees in exactly one of expected / last_published
    on_relay: int
    missing: int  # expected observees with no TA on the relay
    orphans: int  # observees with a TA on the relay that aren't expected
    exact: bool = True  # False: dense seconds were sampled, missing/orphans estimated

    @property
    def needs_resync(self) -> bool:
        return self.missing > 0


@dataclass(frozen=True)
class RelayView:
    total: int
    d_tags: set[str]  # from every window that could be downloaded
    dense_events: int  # TAs in seconds too dense to download
    dense_present: int  # of the expected observees not in d_tags, how many are in those
    exact: bool


class _Relay:
    def __init__(self, ws, signer: str):
        self.ws = ws
        self.base = {"kinds": [TA_KIND], "authors": [signer]}

    async def count(self, **flt) -> int:
        await self.ws.send(json.dumps(["COUNT", "c", {**self.base, **flt}]))
        while True:
            frame = json.loads(await self.ws.recv())
            if frame[0] == "COUNT" and frame[1] == "c":
                return int(frame[2]["count"])

    async def d_tags(self, into: set[str], **flt) -> None:
        await self.ws.send(json.dumps(["REQ", "w", {**self.base, **flt}]))
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


async def _walk(
    relay: _Relay, since: int, until: int, n: int, into: set[str], dense: list[int]
) -> None:
    if n == 0:
        return
    if n <= WINDOW_EVENTS or since == until:
        if n > MAX_SECOND_EVENTS:
            dense.append(n)
            return
        await relay.d_tags(into, since=since, until=until, limit=n)
        return
    mid = (since + until) // 2
    await _walk(
        relay, since, mid, await relay.count(since=since, until=mid), into, dense
    )
    await _walk(
        relay,
        mid + 1,
        until,
        await relay.count(since=mid + 1, until=until),
        into,
        dense,
    )


async def _dense_present(relay: _Relay, candidates: list[str]) -> tuple[int, bool]:
    """How many candidates have a TA (all of them are outside the downloaded windows,
    so any hit is in a dense second). Exact when few, sampled when many."""
    if len(candidates) <= EXACT_DENSE_CANDIDATES:
        present = 0
        for i in range(0, len(candidates), D_TAG_CHUNK):
            present += await relay.count(**{"#d": candidates[i : i + D_TAG_CHUNK]})
        return present, True
    sample = random.sample(candidates, DENSE_SAMPLE)
    found: set[str] = set()
    await relay.d_tags(found, **{"#d": sample, "limit": len(sample)})
    return round(len(candidates) * len(found & set(sample)) / len(sample)), False


async def _read(relay: _Relay, expected: set[str]) -> RelayView:
    until = int(time.time()) + FUTURE_SLACK_SECONDS
    total = await relay.count(since=0, until=until)
    d_tags: set[str] = set()
    dense: list[int] = []
    await _walk(relay, 0, until, total, d_tags, dense)
    dense_present, exact = 0, True
    if dense:
        candidates = sorted(expected - d_tags)
        dense_present, exact = await _dense_present(relay, candidates)
        # a sampled estimate can overshoot; never more than either side holds
        dense_present = min(dense_present, sum(dense), len(candidates))
    return RelayView(total, d_tags, sum(dense), dense_present, exact)


async def read_relay(signer: str, expected: set[str]) -> RelayView:
    async with websockets.connect(
        settings.nostr_upload_ta_events_relay, max_size=None
    ) as ws:
        return await _read(_Relay(ws, signer), expected)


async def audit_observer_relay(db: AsyncDBSession, observer: str) -> RelayAudit:
    nsec_row = await select_brainstorm_nsec_by_pubkey_on_db(db, observer)
    signer = Keys.parse(secret_key=nsec_row.nsec).public_key().to_hex()
    published = set(await get_last_published_pubkeys_by_pubkey_on_db(db, observer))
    async with neo4j_driver.session() as neo:
        expected = await get_ta_observees_for_observer(
            neo, observer, settings.cutoff_of_valid_graperank_scores
        )

    try:
        view = await asyncio.wait_for(
            read_relay(signer, expected), RELAY_TIMEOUT_SECONDS
        )
    except (TimeoutError, OSError, websockets.WebSocketException) as exc:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=f"Could not read the TA relay: {exc.__class__.__name__}",
        )
    not_downloaded = expected - view.d_tags
    return RelayAudit(
        observer=observer,
        signer=signer,
        expected=len(expected),
        published=len(published),
        baseline_drift=len(expected ^ published),
        on_relay=view.total,
        missing=len(not_downloaded) - view.dense_present,
        orphans=len(view.d_tags - expected) + view.dense_events - view.dense_present,
        exact=view.exact,
    )
