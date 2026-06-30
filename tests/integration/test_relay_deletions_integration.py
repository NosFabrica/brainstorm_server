"""Integration test for fetch-free `a`-tag relay deletions (issue 03, AC#2).

Requires the real TA relay (strfry/neofry) reachable at
``settings.nostr_upload_ta_events_relay``, e.g. ``docker compose up -d``. Run::

    poetry run pytest tests/integration -m integration

Proves the behaviour strfry's source promised: a kind-5 deletion carrying an
`a`-tag coordinate ``30382:<signing_pubkey>:<observee>`` removes that Observee's
kind-30382 Trusted Assertion from the relay, while an unrelated TA by the same
Observer remains. No relay fetch is used to build the deletion.
"""

import asyncio
from datetime import timedelta

import pytest
from nostr_sdk import Filter, Keys, Kind, PublicKey

from app.message_queue_tasks.ta_signing import TA_KIND, build_ta_event_builder
from app.message_queue_tasks.upload_nostr_events import (
    get_deletion_events_for_dropped_pubkeys,
    init_nostr_client,
)

pytestmark = pytest.mark.integration


async def _published_dtags(client, author_pubkey: str) -> set[str]:
    flt = Filter().kinds([Kind(TA_KIND)]).authors([PublicKey.parse(author_pubkey)])
    events = await client.fetch_events(flt, timeout=timedelta(seconds=5))
    dtags: set[str] = set()
    for ev in events.to_vec():
        for tag in ev.tags().to_vec():
            vec = tag.as_vec()
            if len(vec) >= 2 and vec[0] == "d":
                dtags.add(vec[1])
    return dtags


def test_atag_deletion_removes_one_ta_and_leaves_others_on_strfry():
    keys = Keys.generate()
    nsec = keys.secret_key().to_bech32()
    pubkey = keys.public_key().to_hex()
    drop = Keys.generate().public_key().to_hex()  # Observee whose TA we delete
    keep = Keys.generate().public_key().to_hex()  # unrelated TA, must survive

    async def _run():
        client = await init_nostr_client(nsec)
        try:
            for observee in (drop, keep):
                ta = build_ta_event_builder(observee, 50, 1).sign_with_keys(keys)
                await client.send_event(ta)
            await asyncio.sleep(0.3)
            assert {drop, keep} <= await _published_dtags(client, pubkey)

            # Fetch-free a-tag deletion for `drop` only.
            deletions = await get_deletion_events_for_dropped_pubkeys(
                [drop], pubkey, client
            )
            for de in deletions:
                await client.send_event(de)
            await asyncio.sleep(0.3)

            remaining = await _published_dtags(client, pubkey)
            assert drop not in remaining  # the targeted TA is gone
            assert keep in remaining  # the unrelated TA survives
        finally:
            # Clean up the surviving TA so the relay isn't left with test data.
            for de in await get_deletion_events_for_dropped_pubkeys(
                [keep], pubkey, client
            ):
                await client.send_event(de)
            await client.disconnect()

    asyncio.run(_run())
