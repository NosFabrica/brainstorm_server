"""Integration test for the reconcile relay STREAM transport (issue 02, AC4).

Requires the real TA relay (strfry/neofry) at ``settings.nostr_upload_ta_events_relay``
(e.g. ``docker compose up -d``). Run with::

    poetry run pytest tests/integration -m integration

Publishes a few TAs, then drives `_stream_relay_into` — the subscription-based,
EOSE-terminated stream that pushes each event into the drift accumulator and
drops it (no buffering) — and asserts the classification matches the desired
state. Isolates the streaming transport: no Neo4j / Vespa needed.
"""

import asyncio

import pytest
from nostr_sdk import ClientMessage, Keys

from app.message_queue_tasks.ta_signing import (
    build_atag_deletion_builders,
    build_ta_event_builder,
)
from app.message_queue_tasks.upload_nostr_events import init_nostr_client
from app.services.reconcile import RelayDriftAccumulator
from app.services.reconcile_service import _stream_relay_into

pytestmark = pytest.mark.integration


def test_stream_relay_classifies_published_tas_against_desired():
    keys = Keys.generate()
    nsec = keys.secret_key().to_bech32()
    signer_pubkey = keys.public_key().to_hex()
    # Fresh pubkeys as d-tags so this run never collides with another on the
    # shared relay.
    match = Keys.generate().public_key().to_hex()
    drift = Keys.generate().public_key().to_hex()
    ghost = Keys.generate().public_key().to_hex()
    missing = Keys.generate().public_key().to_hex()

    async def _run():
        client = await init_nostr_client(nsec)
        try:
            # Published actual: match@90, drift@47 (desired wants 50), ghost@10
            # (not desired). `missing` is desired but never published.
            for observee, rank in ((match, 90), (drift, 47), (ghost, 10)):
                await client.send_event(
                    build_ta_event_builder(observee, rank, 0).sign_with_keys(keys)
                )
            await asyncio.sleep(0.3)

            desired = {match: 90, drift: 50, missing: 30}
            acc = RelayDriftAccumulator(desired)
            await _stream_relay_into(acc, client, signer_pubkey)
            result = acc.result()

            assert result.stale == [(drift, 50)]  # republish at desired rank
            assert result.ghost == [ghost]  # not desired → delete
            assert result.missing == [(missing, 30)]  # never seen → publish
        finally:
            write_relays = list((await client.relays()).values())
            for builder in build_atag_deletion_builders(
                [match, drift, ghost], signer_pubkey
            ):
                msg = ClientMessage.event(builder.sign_with_keys(keys))
                for relay in write_relays:
                    relay.send_msg(msg)
            await asyncio.sleep(0.2)
            await client.disconnect()

    asyncio.run(_run())
