"""Fetch-free `a`-tag relay deletions (issue 03).

Exercises the pure deletion-event construction (`ta_signing`) and the local-diff
relay delete-set logic (`upload_nostr_events`) — no relay round-trip.
"""

import asyncio
from unittest.mock import AsyncMock, MagicMock

from nostr_sdk import Event, Keys

from app.message_queue_tasks.ta_signing import build_atag_deletion_builders
from app.message_queue_tasks.upload_nostr_events import (
    compute_delete_observees,
    get_deletion_events_for_dropped_pubkeys,
)


def _a_tags(event: Event) -> list[str]:
    return [t.as_vec()[1] for t in event.tags().to_vec() if t.as_vec()[0] == "a"]


def _fake_client(keys: Keys) -> MagicMock:
    """A relay client stub whose `sign_event_builder` signs locally — and which
    has no fetch method, so any attempt to round-trip the relay raises."""
    client = MagicMock(spec=["sign_event_builder"])

    async def _sign(builder):
        return builder.sign_with_keys(keys)

    client.sign_event_builder = AsyncMock(side_effect=_sign)
    return client


def test_build_atag_deletion_builders_emits_kind5_with_coordinate_a_tags():
    keys = Keys.generate()
    pubkey = keys.public_key().to_hex()

    builders = build_atag_deletion_builders(["observee-a", "observee-b"], pubkey)

    assert len(builders) == 1  # both coordinates fit one deletion event
    event = builders[0].sign_with_keys(keys)
    assert event.kind().as_u16() == 5
    assert _a_tags(event) == [
        f"30382:{pubkey}:observee-a",
        f"30382:{pubkey}:observee-b",
    ]


def test_build_atag_deletion_builders_chunks_and_covers_every_observee_once():
    keys = Keys.generate()
    pubkey = keys.public_key().to_hex()
    observees = [f"o{i:03d}" for i in range(5)]

    builders = build_atag_deletion_builders(observees, pubkey, chunk_size=2)

    assert len(builders) == 3  # 2 + 2 + 1
    events = [b.sign_with_keys(keys) for b in builders]
    assert [len(_a_tags(e)) for e in events] == [2, 2, 1]
    covered = [coord.split(":")[2] for e in events for coord in _a_tags(e)]
    assert covered == observees  # each once, in order


def test_build_atag_deletion_builders_signed_event_and_a_tag_share_signing_pubkey():
    # strfry only deletes when the a-tag pubkey == the deletion event's author;
    # guard that the coordinate uses the signing pubkey, not the Observer's own.
    keys = Keys.generate()
    pubkey = keys.public_key().to_hex()

    event = build_atag_deletion_builders(["observee-a"], pubkey)[0].sign_with_keys(keys)

    assert event.author().to_hex() == pubkey
    assert _a_tags(event)[0].split(":")[1] == pubkey


def test_incremental_delete_set_is_previously_minus_currently_published():
    # "b" stayed (still published) → kept; "x" fell off (was published, now gone)
    # → deleted; "c" is newly above-cutoff → never published, nothing to delete.
    observees = compute_delete_observees(
        previously_published=["b", "x"],
        currently_published=["b", "c"],
        below_cutoff=[],
        full_sync=False,
    )

    assert observees == ["x"]


def test_full_sync_delete_set_also_sweeps_all_below_cutoff():
    # Same diff as above ("x"), plus full-sync reconciliation sweeps every
    # below-cutoff Observee ("lo") even though it was never in previously/currently.
    observees = compute_delete_observees(
        previously_published=["b", "x"],
        currently_published=["b", "c"],
        below_cutoff=["lo"],
        full_sync=True,
    )

    assert observees == ["lo", "x"]  # sorted union, deduped


def test_delete_set_is_empty_when_nothing_dropped():
    assert (
        compute_delete_observees(
            previously_published=["b"],
            currently_published=["b", "c"],
            below_cutoff=[],
            full_sync=False,
        )
        == []
    )


def test_get_deletion_events_builds_signed_atag_kind5_without_fetch():
    keys = Keys.generate()
    pubkey = keys.public_key().to_hex()
    client = _fake_client(keys)

    events = asyncio.run(
        get_deletion_events_for_dropped_pubkeys(["x", "y"], pubkey, client)
    )

    assert len(events) == 1
    assert events[0].kind().as_u16() == 5
    assert events[0].author().to_hex() == pubkey
    assert _a_tags(events[0]) == [f"30382:{pubkey}:x", f"30382:{pubkey}:y"]


def test_get_deletion_events_is_empty_for_no_observees():
    client = _fake_client(Keys.generate())

    events = asyncio.run(get_deletion_events_for_dropped_pubkeys([], "pk", client))

    assert events == []
    client.sign_event_builder.assert_not_called()
