"""End-to-end publish + retraction against the isolated local relay (AC13, AC14).

Runs the real service: Postgres taggings, Neo4j ranks, and a live strfry. This
is the only level at which the retraction pass is observable — the pure
`plan_retractions` unit tests cannot see whether it is ever *called*.
"""
from __future__ import annotations

import asyncio
import json
from datetime import timedelta

import pytest
from nostr_sdk import Client, Filter, Keys, Kind, PublicKey
from sqlalchemy import delete

from app.core.config import settings
from app.core.database import async_session_factory, engine
from app.db_models import NostrTagElement, NostrUserTagging
from app.repos.tagging_repo import (
    upsert_tag_element_on_db,
    upsert_user_tagging_on_db,
)
from app.services.tagging_parse import TagElement, UserTagging
import app.services.trusted_list_service as svc_module
from app.services.trusted_list_build import TRUSTED_LIST_KIND, compute_d_tag
from tests.integration.tagging_harness import neo_driver, seed_influence

pytestmark = pytest.mark.integration

TAG_AUTHOR = "8" * 64
TAG_EV = "7" * 64
TARGET = "9" * 64


async def _read_slots(signing_pubkey: str) -> dict[str, dict]:
    """Every kind-30392 this signer has on the relay, by d-tag."""
    client = Client()
    await client.add_relay(settings.nostr_upload_ta_events_relay)
    await client.connect()
    try:
        flt = (
            Filter()
            .kinds([Kind(TRUSTED_LIST_KIND)])
            .authors([PublicKey.parse(signing_pubkey)])
            .limit(500)
        )
        events = await client.fetch_events(flt, timeout=timedelta(seconds=10))
        out: dict[str, dict] = {}
        for ev in events.to_vec():
            raw = json.loads(ev.as_json())
            tags = raw.get("tags", [])
            d = next((t[1] for t in tags if t and t[0] == "d"), None)
            if not d:
                continue
            prev = out.get(d)
            if prev and prev["created_at"] > raw["created_at"]:
                continue
            out[d] = {
                "created_at": raw["created_at"],
                "members": [t[1] for t in tags if t and t[0] == "p"],
                "retracted": any(
                    t[0] == "status" and t[1] == "retracted" for t in tags if t
                ),
                "title": next((t[1] for t in tags if t and t[0] == "title"), None),
                "description": next(
                    (t[1] for t in tags if t and t[0] == "description"), None
                ),
            }
        return out
    finally:
        await client.shutdown()


def _element():
    return TagElement(
        event_id=TAG_EV,
        author_pubkey=TAG_AUTHOR,
        slug="podcaster",
        name="Podcaster",
        description="Makes podcasts",
        created_at_unix=100,
    )


def _tagging(asserter):
    return UserTagging(
        event_id=asserter[:32] + TAG_EV[:32],
        asserter_pubkey=asserter,
        d_tag=f"dt-{asserter[:6]}",
        target_pubkey=TARGET,
        tag_event_id=TAG_EV,
        polarity=1.0,
        created_at_unix=100,
    )


TAG_EV2 = "6" * 64


def _element2():
    return TagElement(
        event_id=TAG_EV2,
        author_pubkey=TAG_AUTHOR,
        slug="chef",
        name="Chef",
        description="Cooks",
        created_at_unix=100,
    )


def _tagging2(asserter):
    return UserTagging(
        event_id=asserter[:32] + TAG_EV2[:32],
        asserter_pubkey=asserter,
        d_tag=f"dt2-{asserter[:6]}",
        target_pubkey=TARGET,
        tag_event_id=TAG_EV2,
        polarity=1.0,
        created_at_unix=100,
    )


def test_publish_retract_idempotence_and_observer_scoping():
    """AC13. The commonest retraction case: the tag stops qualifying entirely,
    so the dictionary is empty on the second run — which must still retract,
    not return early."""
    from app.services.trusted_list_service import (
        generate_trusted_lists_for_observer,
    )

    observer = Keys.generate().public_key().to_hex()
    observer_y = Keys.generate().public_key().to_hex()
    asserter = Keys.generate().public_key().to_hex()
    d_tag = compute_d_tag(observer, TAG_AUTHOR, "podcaster")

    async def _go(fresh_neo):
        # Both the asyncpg pool and the Neo4j driver are module-level
        # singletons that bind to the first event loop touching them. Other
        # integration tests bind them first, so this test disposes the pool and
        # runs the service against a Neo4j driver created inside THIS loop.
        await engine.dispose()
        svc_module.neo4j_driver = fresh_neo
        await seed_influence({asserter: 0.9}, observer)
        await seed_influence({asserter: 0.9}, observer_y)
        try:
            async with async_session_factory() as db:
                await db.execute(delete(NostrUserTagging))
                await db.execute(delete(NostrTagElement))
                await upsert_tag_element_on_db(db, _element())
                await upsert_user_tagging_on_db(db, _tagging(asserter))
                await db.commit()

            first = await generate_trusted_lists_for_observer(observer)
            y_first = await generate_trusted_lists_for_observer(observer_y)
            await asyncio.sleep(1)
            after_publish = await _read_slots(first.signing_pubkey)

            # The SAME qualifying asserter moves to a different tag. The view
            # stays trustworthy (taggings exist, asserters qualify) but the
            # podcaster slot is now stale and must be retracted.
            async with async_session_factory() as db:
                await db.execute(delete(NostrUserTagging))
                await upsert_tag_element_on_db(db, _element2())
                await upsert_user_tagging_on_db(db, _tagging2(asserter))
                await db.commit()

            second = await generate_trusted_lists_for_observer(observer)
            third = await generate_trusted_lists_for_observer(observer)
            await asyncio.sleep(1)
            after_retract = await _read_slots(first.signing_pubkey)
            y_slots = await _read_slots(y_first.signing_pubkey)
            return first, after_publish, second, third, after_retract, y_slots
        finally:
            async with async_session_factory() as db:
                await db.execute(delete(NostrUserTagging))
                await db.execute(delete(NostrTagElement))
                await db.commit()
            drv = neo_driver()
            async with drv.session() as s:
                await s.run("MATCH (u:NostrUser) DETACH DELETE u")
            await drv.close()
            await engine.dispose()

    async def _outer():
        fresh_neo = neo_driver()
        original = svc_module.neo4j_driver
        try:
            return await _go(fresh_neo)
        finally:
            svc_module.neo4j_driver = original
            await fresh_neo.close()

    first, after_publish, second, third, after_retract, y_slots = asyncio.run(_outer())

    # Run 1 published a real list with the target as a member.
    assert first.published == 1
    assert d_tag in after_publish
    assert after_publish[d_tag]["members"] == [TARGET]
    assert after_publish[d_tag]["retracted"] is False
    # AC9: title AND description ride the published event.
    assert after_publish[d_tag]["title"] == "Podcaster"
    assert after_publish[d_tag]["description"] == "Makes podcasts"

    # Run 2 retracted it: empty membership + the marker, same coordinate.
    assert second.published == 1  # the new tag (chef)
    assert second.retracted == 1
    assert after_retract[d_tag]["retracted"] is True
    assert after_retract[d_tag]["members"] == []

    # --- idempotence + cross-observer scoping (AC13, S3) --------------------
    # Folded into this one test deliberately: the service holds module-level
    # Neo4j and asyncpg singletons that bind to the first event loop that
    # touches them, so a second `asyncio.run` in this file fails with
    # "attached to a different loop". One loop, one scenario.
    assert third.retracted == 0, "second retraction pass must be a no-op"
    y_d = compute_d_tag(observer_y, TAG_AUTHOR, "podcaster")
    assert y_slots[y_d]["retracted"] is False
    assert y_slots[y_d]["members"] == [TARGET]
