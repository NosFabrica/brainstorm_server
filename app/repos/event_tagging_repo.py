"""Data access for event taggings, tagging headers and the by-id lookup cache.

ADR `trusted-lists/0002` D6. Replaceability is enforced at write time, as in
`tagging_repo`: both upserts are latest-wins on the event's own `created_at`,
so an apply -> dispute flip replaces the stance and reads are plain selects.
`tagging_repo` keeps tag elements and pubkey taggings.
"""
from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy import String, and_, any_, bindparam, func, literal, or_, select
from sqlalchemy.dialects.postgresql import ARRAY
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession as AsyncDBSession

from app.db_models import (
    NostrEventTagging,
    NostrTaggedEvent,
    NostrTaggingHeader,
    NostrTagElement,
)
from app.services.tagging_parse import TAGGING_KIND, EventTagging, TaggingHeader


@dataclass(frozen=True)
class EventTaggingRow:
    """One live event tagging, joined through its header to its Tag element."""

    asserter_pubkey: str
    target: str
    target_slot: str
    target_relay: str | None
    polarity: float
    created_at_unix: int
    tag_event_id: str
    tag_author_pubkey: str
    slug: str
    name: str
    description: str


@dataclass(frozen=True)
class TaggedEvent:
    """What a lookup by id learned about one event. Immutable, so cacheable."""

    event_id: str
    kind: int
    pubkey: str
    d_tag: str | None
    z_values: tuple[str, ...]
    names: tuple[str, ...]


async def upsert_tagging_header_on_db(db: AsyncDBSession, header: TaggingHeader) -> None:
    """Insert or replace a tagging header at its coordinate, latest-wins."""
    values = dict(
        event_id=header.event_id,
        tag_author_pubkey=header.tag_author_pubkey,
        tag_slug=header.tag_slug,
        tag_event_id=header.tag_event_id,
        created_at_unix=header.created_at_unix,
    )
    stmt = (
        pg_insert(NostrTaggingHeader)
        .values(author_pubkey=header.author_pubkey, d_tag=header.d_tag, **values)
        .on_conflict_do_update(
            index_elements=["author_pubkey", "d_tag"],
            set_=values,
            where=NostrTaggingHeader.created_at_unix < header.created_at_unix,
        )
    )
    await db.execute(stmt)


async def upsert_event_tagging_on_db(db: AsyncDBSession, tagging: EventTagging) -> None:
    """Insert or replace an event tagging at (asserter, d-tag), latest-wins."""
    values = dict(
        event_id=tagging.event_id,
        target=tagging.target,
        target_slot=tagging.target_slot,
        target_relay=tagging.target_relay,
        header_coordinate=tagging.header_coordinate,
        polarity=tagging.polarity,
        created_at_unix=tagging.created_at_unix,
    )
    stmt = (
        pg_insert(NostrEventTagging)
        .values(asserter_pubkey=tagging.asserter_pubkey, d_tag=tagging.d_tag, **values)
        .on_conflict_do_update(
            index_elements=["asserter_pubkey", "d_tag"],
            set_=values,
            where=NostrEventTagging.created_at_unix < tagging.created_at_unix,
        )
    )
    await db.execute(stmt)


async def count_event_taggings_on_db(db: AsyncDBSession) -> int:
    """Event taggings held. Zero means nothing was ever ingested: a run then
    treats its content view as untrustworthy and retracts no content list."""
    result = await db.execute(select(func.count()).select_from(NostrEventTagging))
    return int(result.scalar_one())


async def get_event_tagging_asserters_on_db(db: AsyncDBSession) -> list[str]:
    """Every distinct asserter of an event tagging."""
    result = await db.execute(select(NostrEventTagging.asserter_pubkey).distinct())
    return [row[0] for row in result.all()]


async def get_event_taggings_on_db(
    db: AsyncDBSession, qualifying_asserters: list[str]
) -> list[EventTaggingRow]:
    """Every live event tagging by a qualifying asserter whose header and Tag
    element are both held.

    The joins are the read-time drop (D6): a tagging whose header or Tag hasn't
    arrived yet is kept at write and simply doesn't appear here until it has.
    Neutral polarities ride along; the caller needs them, because a newest
    neutral stance still replaces an older apply (one stance per asserter).
    """
    if not qualifying_asserters:
        return []
    header_coordinate = (
        literal(f"{TAGGING_KIND}:")
        + NostrTaggingHeader.author_pubkey
        + literal(":")
        + NostrTaggingHeader.d_tag
    )
    stmt = (
        select(
            NostrEventTagging.asserter_pubkey,
            NostrEventTagging.target,
            NostrEventTagging.target_slot,
            NostrEventTagging.target_relay,
            NostrEventTagging.polarity,
            NostrEventTagging.created_at_unix,
            NostrTagElement.event_id,
            NostrTagElement.author_pubkey,
            NostrTagElement.slug,
            NostrTagElement.name,
            NostrTagElement.description,
        )
        .join(
            NostrTaggingHeader,
            NostrEventTagging.header_coordinate == header_coordinate,
        )
        .join(
            NostrTagElement,
            or_(
                and_(
                    NostrTaggingHeader.tag_author_pubkey == NostrTagElement.author_pubkey,
                    NostrTaggingHeader.tag_slug == NostrTagElement.slug,
                ),
                NostrTaggingHeader.tag_event_id == NostrTagElement.event_id,
            ),
        )
        .where(
            NostrEventTagging.asserter_pubkey
            == any_(bindparam("asserters", qualifying_asserters, type_=ARRAY(String)))
        )
    )
    result = await db.execute(stmt)
    return [
        EventTaggingRow(
            asserter_pubkey=row[0],
            target=row[1],
            target_slot=row[2],
            target_relay=row[3],
            polarity=float(row[4]),
            created_at_unix=int(row[5]),
            tag_event_id=row[6],
            tag_author_pubkey=row[7],
            slug=row[8],
            name=row[9],
            description=row[10],
        )
        for row in result.all()
    ]


async def get_tagged_events_on_db(
    db: AsyncDBSession, event_ids: list[str]
) -> dict[str, TaggedEvent]:
    """The cached by-id lookups among `event_ids`."""
    if not event_ids:
        return {}
    stmt = select(NostrTaggedEvent).where(
        NostrTaggedEvent.event_id
        == any_(bindparam("ids", event_ids, type_=ARRAY(String)))
    )
    rows = (await db.execute(stmt)).scalars().all()
    return {
        row.event_id: TaggedEvent(
            event_id=row.event_id,
            kind=row.kind,
            pubkey=row.pubkey,
            d_tag=row.d_tag,
            z_values=tuple(row.z_values or ()),
            names=tuple(row.names or ()),
        )
        for row in rows
    }


async def insert_tagged_events_on_db(
    db: AsyncDBSession, events: list[TaggedEvent]
) -> None:
    """Cache by-id lookups. An id already held is left alone: it can't change."""
    if not events:
        return
    stmt = (
        pg_insert(NostrTaggedEvent)
        .values(
            [
                dict(
                    event_id=e.event_id,
                    kind=e.kind,
                    pubkey=e.pubkey,
                    d_tag=e.d_tag,
                    z_values=list(e.z_values),
                    names=list(e.names),
                )
                for e in events
            ]
        )
        .on_conflict_do_nothing(index_elements=["event_id"])
    )
    await db.execute(stmt)
