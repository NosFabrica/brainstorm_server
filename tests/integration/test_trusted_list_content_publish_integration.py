"""End-to-end Trusted Lists of tagged content (story 2, AC6, AC13, AC15, AC16;
ADR `trusted-lists/0002` D6, D8).

The real service, real Postgres and Neo4j, and two isolated strfry relays: the
tagged events (a note, a DList item and its header) are published to the
transfer relay, which stands in for the lookup relay, and the lists are
published to the Trusted List relay and read back from it.

One scenario in one event loop, for the reason story 1's publish test gives:
the service's asyncpg pool and Neo4j driver bind to the first loop that
touches them.
"""
from __future__ import annotations

import asyncio
import json
from datetime import timedelta

import pytest
from nostr_sdk import (
    Client,
    EventBuilder,
    Filter,
    Keys,
    Kind,
    NostrSigner,
    PublicKey,
    Tag,
)
from sqlalchemy import delete, select

import app.services.trusted_list_service as svc_module
from app.core.config import settings
from app.core.database import async_session_factory, engine
from app.db_models import NostrEventTagging, NostrTaggedEvent
from app.repos.event_tagging_repo import (
    upsert_event_tagging_on_db,
    upsert_tagging_header_on_db,
)
from app.repos.tagging_repo import upsert_tag_element_on_db
from app.services import tagging_target_service as tts
from app.services.tagging_parse import EventTagging, TagElement, TaggingHeader
from app.services.trusted_list_build import hash8
from tests.integration.tagging_harness import (
    clean_tagging_tables,
    neo_driver,
    seed_influence,
)

pytestmark = pytest.mark.integration

LOOKUP_RELAY = settings.nostr_transfer_from_relay
LIST_RELAY = settings.trusted_list_relay or settings.nostr_upload_ta_events_relay


async def _publish_to(relay: str, keys: Keys, builder: EventBuilder) -> str:
    client = Client(signer=NostrSigner.keys(keys=keys))
    await client.add_relay(relay)
    await client.connect()
    try:
        event = await client.sign_event_builder(builder)
        await client.send_event(event)
        return event.id().to_hex()
    finally:
        await client.shutdown()


async def _read_lists(signing_pubkey: str) -> dict[tuple[int, str], dict]:
    """The newest 30393 / 30394 per (kind, d) this signer has on the list relay."""
    client = Client()
    await client.add_relay(LIST_RELAY)
    await client.connect()
    try:
        flt = (
            Filter()
            .kinds([Kind(30393), Kind(30394)])
            .authors([PublicKey.parse(signing_pubkey)])
            .limit(500)
        )
        events = await client.fetch_events(flt, timeout=timedelta(seconds=10))
        out: dict[tuple[int, str], dict] = {}
        for ev in events.to_vec():
            raw = json.loads(ev.as_json())
            tags = raw["tags"]
            d = next(t[1] for t in tags if t[0] == "d")
            key = (raw["kind"], d)
            if key in out and out[key]["created_at"] >= raw["created_at"]:
                continue
            out[key] = {
                "created_at": raw["created_at"],
                "author": raw["pubkey"],
                "title": next((t[1] for t in tags if t[0] == "title"), None),
                "members": [t[1] for t in tags if t[0] in ("e", "a")],
                "retracted": ["status", "retracted"] in tags,
            }
        return out
    finally:
        await client.shutdown()


def test_content_lists_publish_cache_retract_and_empty():
    from app.services.trusted_list_service import generate_trusted_lists_for_observer

    observer = Keys.generate().public_key().to_hex()
    asserter = Keys.generate().public_key().to_hex()
    tag_author = Keys.generate()
    curator = Keys.generate()
    writer = Keys.generate()
    ta = tag_author.public_key().to_hex()
    cu = curator.public_key().to_hex()
    books = f"39998:{cu}:books"
    item = f"39999:{cu}:dune"
    header_coordinate = f"39999:{ta}:tagging:espresso-tagging"

    def tagging(d, target, slot, polarity=1.0, created_at=100):
        return EventTagging(
            event_id=Keys.generate().public_key().to_hex(),
            asserter_pubkey=asserter,
            d_tag=d,
            target=target,
            target_slot=slot,
            target_relay=None,
            header_coordinate=header_coordinate,
            polarity=polarity,
            created_at_unix=created_at,
        )

    fetches: list[str] = []
    real_fetch = tts._fetch_from_relay

    async def counting_fetch(url, filters):
        fetches.extend(f.as_json() for f in filters)
        return await real_fetch(url, filters)

    async def _go(fresh_neo):
        await engine.dispose()
        svc_module.neo4j_driver = fresh_neo
        settings.trusted_list_target_relays = [LOOKUP_RELAY]
        tts._fetch_from_relay = counting_fetch
        await seed_influence({asserter: 0.9}, observer)
        try:
            # The tagged events, on the lookup relay.
            note = await _publish_to(
                LOOKUP_RELAY, writer, EventBuilder(Kind(1), "an espresso note")
            )
            await _publish_to(
                LOOKUP_RELAY,
                curator,
                EventBuilder(Kind(39998), "").tags(
                    [Tag.parse(["d", "books"]), Tag.parse(["names", "Book", "Books"])]
                ),
            )
            await _publish_to(
                LOOKUP_RELAY,
                curator,
                EventBuilder(Kind(39999), "").tags(
                    [Tag.parse(["d", "dune"]), Tag.parse(["z", books])]
                ),
            )
            await asyncio.sleep(0.5)

            async with async_session_factory() as db:
                await clean_tagging_tables(db)
                await upsert_tag_element_on_db(
                    db,
                    TagElement(
                        event_id="7" * 64,
                        author_pubkey=ta,
                        slug="espresso",
                        name="espresso",
                        description="Coffee, pressed",
                        created_at_unix=100,
                    ),
                )
                await upsert_tagging_header_on_db(
                    db,
                    TaggingHeader(
                        event_id="8" * 64,
                        author_pubkey=ta,
                        d_tag="tagging:espresso-tagging",
                        tag_author_pubkey=ta,
                        tag_slug="espresso",
                        tag_event_id=None,
                        created_at_unix=100,
                    ),
                )
                await upsert_event_tagging_on_db(db, tagging("on-note", note, "e"))
                await upsert_event_tagging_on_db(db, tagging("on-item", item, "a"))
                await db.commit()

            first = await generate_trusted_lists_for_observer(observer)
            await asyncio.sleep(1)
            after_first = await _read_lists(first.signing_pubkey)
            # The same read under any other key finds none of them (I6).
            wrong_signer = await _read_lists(Keys.generate().public_key().to_hex())
            async with async_session_factory() as db:
                cached = (await db.execute(select(NostrTaggedEvent.event_id))).scalars().all()

            # Run 2: the note's answer comes from the cache; the item is
            # fetched by coordinate again.
            fetches.clear()
            second = await generate_trusted_lists_for_observer(observer)
            second_fetches = list(fetches)

            # Run 3: the note tagging goes away (its pair leaves the
            # dictionary), and the item is disputed (its pair stays, emptied).
            # A replaceable event is ordered by `created_at` in seconds, so a
            # retraction in the same second as the list it replaces can lose
            # to it on the relay. Admin runs are never that close; tests are.
            await asyncio.sleep(1.1)
            async with async_session_factory() as db:
                await db.execute(
                    delete(NostrEventTagging).where(NostrEventTagging.d_tag == "on-note")
                )
                await upsert_event_tagging_on_db(
                    db, tagging("on-item", item, "a", polarity=-1.0, created_at=200)
                )
                await db.commit()
            third = await generate_trusted_lists_for_observer(observer)
            fourth = await generate_trusted_lists_for_observer(observer)
            await asyncio.sleep(1)
            after_third = await _read_lists(first.signing_pubkey)
            return (
                note,
                first,
                after_first,
                wrong_signer,
                cached,
                second,
                second_fetches,
                third,
                fourth,
                after_third,
            )
        finally:
            tts._fetch_from_relay = real_fetch
            async with async_session_factory() as db:
                await clean_tagging_tables(db)
            drv = neo_driver()
            async with drv.session() as s:
                await s.run("MATCH (u:NostrUser {pubkey: $pk}) DETACH DELETE u", pk=asserter)
            await drv.close()
            await engine.dispose()

    async def _outer():
        fresh_neo = neo_driver()
        original_neo = svc_module.neo4j_driver
        original_relays = settings.trusted_list_target_relays
        try:
            return await _go(fresh_neo)
        finally:
            svc_module.neo4j_driver = original_neo
            settings.trusted_list_target_relays = original_relays
            await fresh_neo.close()

    (
        note,
        first,
        after_first,
        wrong_signer,
        cached,
        second,
        second_fetches,
        third,
        fourth,
        after_third,
    ) = asyncio.run(_outer())

    obs8, ta8, cu8 = observer[:8], ta[:8], cu[:8]
    notes_d = (30393, f"tl-tag-notes-{obs8}-{ta8}-espresso-k1")
    items_d = (30394, f"tl-tag-items-{obs8}-{ta8}-espresso-dl{cu8}-books-{hash8(books)}")

    # Run 1 (AC13): both lists, signed by the Observer's assistant, on the
    # Trusted List relay, titled from the kind table and the DList's names.
    assert first.content_skipped_reason is None
    assert first.published == 2 and first.unresolved_targets == 0
    assert after_first[notes_d]["title"] == "espresso Notes"
    assert after_first[notes_d]["members"] == [note]
    assert after_first[items_d]["title"] == "espresso Books"
    assert after_first[items_d]["members"] == [item]
    assert {v["author"] for v in after_first.values()} == {first.signing_pubkey}
    assert wrong_signer == {}

    # AC6 / I3: the by-id answer was cached; run 2 didn't ask for it again, but
    # did fetch the item by coordinate.
    assert note in cached
    assert second.published == 2
    assert not any(note in f for f in second_fetches)
    assert any('"#d":["dune"]' in f for f in second_fetches)

    # Run 3 (AC15, AC16): the note's list retracted; the item's list emptied
    # but not retracted. Run 4 retracts nothing more.
    assert third.retracted == 1
    assert after_third[notes_d]["retracted"] is True
    assert after_third[notes_d]["members"] == []
    assert after_third[items_d]["retracted"] is False
    assert after_third[items_d]["members"] == []
    assert fourth.retracted == 0
