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

import re
from datetime import timedelta

from nostr_sdk import Client, Filter, Kind, PublicKey  # type: ignore

from app.core.config import settings
from app.core.loggr import loggr

logger = loggr.get_logger(__name__)

DESIGNATION_KIND = 10040

# `<kind>` or `<kind>:<metric>`, e.g. "30382:rank", "30392".
_DESIGNATION_TAG_NAME = re.compile(r"^\d+(:.*)?$")
_HEX_PUBKEY = re.compile(r"^[0-9a-f]{64}$")

_FETCH_TIMEOUT = timedelta(seconds=5)


def parse_designated_pubkeys(tags: list[list[str]], observer: str) -> list[str]:
    """Provider pubkeys named by a kind-10040's designation rows, in order,
    de-duplicated, never including the Observer."""
    seen: set[str] = set()
    out: list[str] = []
    for tag in tags:
        if len(tag) < 2 or not _DESIGNATION_TAG_NAME.match(tag[0]):
            continue
        pubkey = tag[1].lower()
        if not _HEX_PUBKEY.match(pubkey) or pubkey == observer or pubkey in seen:
            continue
        seen.add(pubkey)
        out.append(pubkey)
    return out


async def _fetch_latest_designation(relay: str, observer: str):
    """The latest kind-10040 `relay` holds for `observer`, or None. Raises on
    transport errors."""
    fetcher = Client()
    await fetcher.add_relay(relay)
    await fetcher.connect()
    try:
        flt = (
            Filter()
            .kinds([Kind(DESIGNATION_KIND)])
            .authors([PublicKey.parse(observer)])
            .limit(1)
        )
        events = (await fetcher.fetch_events(flt, timeout=_FETCH_TIMEOUT)).to_vec()
    finally:
        await fetcher.disconnect()
    if not events:
        return None
    return max(events, key=lambda e: e.created_at().as_secs())


async def fetch_designated_pubkeys(observer: str) -> list[str]:
    """The pubkeys in `observer`'s latest kind-10040, or `[]` when there is none
    or it can't be read.

    Reads our own relay first — the transferer and the router stream both copy
    kind 10040 into it. Falls back to the upstream relay for an Observer our
    relay has nothing for yet (e.g. while the initial 10040 backfill runs).
    """
    for relay in (settings.nostr_transfer_to_relay, settings.nostr_transfer_from_relay):
        try:
            event = await _fetch_latest_designation(relay, observer)
        except Exception as e:
            logger.warning(f"kind-10040 lookup on {relay} failed for {observer}: {e!r}")
            continue
        if event is not None:
            tags = [tag.as_vec() for tag in event.tags().to_vec()]
            return parse_designated_pubkeys(tags, observer)
    return []
