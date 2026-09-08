"""Data access for kind-39999 tag elements and taggings.

Replaceability (latest-wins on the event's own `created_at`) is enforced HERE,
at write time, so every read is a plain aggregate. See ADR
`trusted-lists/0001` D2 for why this isn't a read-time dedupe pass.
"""
from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession as AsyncDBSession

from app.core.loggr import loggr
from app.db_models import NostrTagElement, NostrUserTagging
from app.services.tagging_parse import TagElement, UserTagging

logger = loggr.get_logger(__name__)


@dataclass(frozen=True)
class DictionaryEntry:
    """One entry in an Observer's dictionary: a tag element plus its usage."""

    tag_event_id: str
    tag_author_pubkey: str
    slug: str
    name: str
    description: str
    # Distinct qualifying asserters who used this tag at all (apply or dispute).
    uses: int


async def upsert_tag_element_on_db(db: AsyncDBSession, element: TagElement) -> None:
    """Insert or replace a tag element at its addressable coordinate.

    Latest-wins on `created_at_unix`: an out-of-order older event is ignored.
    """
    stmt = (
        pg_insert(NostrTagElement)
        .values(
            event_id=element.event_id,
            author_pubkey=element.author_pubkey,
            slug=element.slug,
            name=element.name,
            description=element.description,
            created_at_unix=element.created_at_unix,
        )
        .on_conflict_do_update(
            constraint="uq_nostr_tag_element_coordinate",
            set_={
                "event_id": element.event_id,
                "name": element.name,
                "description": element.description,
                "created_at_unix": element.created_at_unix,
            },
            where=NostrTagElement.created_at_unix < element.created_at_unix,
        )
    )
    await db.execute(stmt)


async def upsert_user_tagging_on_db(db: AsyncDBSession, tagging: UserTagging) -> None:
    """Insert or replace an assertion at (asserter, d-tag).

    Latest-wins on `created_at_unix`, which is what makes an apply -> dispute
    flip replace the prior stance instead of double-counting it.
    """
    stmt = (
        pg_insert(NostrUserTagging)
        .values(
            asserter_pubkey=tagging.asserter_pubkey,
            d_tag=tagging.d_tag,
            event_id=tagging.event_id,
            target_pubkey=tagging.target_pubkey,
            tag_event_id=tagging.tag_event_id,
            polarity=tagging.polarity,
            created_at_unix=tagging.created_at_unix,
        )
        .on_conflict_do_update(
            index_elements=["asserter_pubkey", "d_tag"],
            set_={
                "event_id": tagging.event_id,
                "target_pubkey": tagging.target_pubkey,
                "tag_event_id": tagging.tag_event_id,
                "polarity": tagging.polarity,
                "created_at_unix": tagging.created_at_unix,
            },
            where=NostrUserTagging.created_at_unix < tagging.created_at_unix,
        )
    )
    await db.execute(stmt)


async def count_taggings_on_db(db: AsyncDBSession) -> int:
    """Total taggings held. Drives AC15's empty-store-vs-no-qualifiers signal:
    zero here means nothing was ever ingested (the un-synced-relay case)."""
    result = await db.execute(select(func.count()).select_from(NostrUserTagging))
    return int(result.scalar_one())


async def get_asserter_pubkeys_on_db(db: AsyncDBSession) -> list[str]:
    """Every distinct asserter. The caller scores these against one Observer's
    web of trust, then feeds the qualifying set back in."""
    result = await db.execute(select(NostrUserTagging.asserter_pubkey).distinct())
    return [row[0] for row in result.all()]


async def get_dictionary_on_db(
    db: AsyncDBSession, qualifying_asserters: list[str], min_uses: int
) -> list[DictionaryEntry]:
    """The Observer's dictionary: tag elements used at least `min_uses` times by
    a qualifying asserter.

    Joins to the element table, so a tagging referencing an element we never
    ingested is dropped (S2) rather than yielding a TL with an empty title.
    Neutral-polarity assertions are excluded — they count as neither use.
    """
    if not qualifying_asserters:
        return []

    from app.services.tagging_parse import APPLY_THRESHOLD, DISPUTE_THRESHOLD

    uses = func.count(func.distinct(NostrUserTagging.asserter_pubkey)).label("uses")
    stmt = (
        select(
            NostrTagElement.event_id,
            NostrTagElement.author_pubkey,
            NostrTagElement.slug,
            NostrTagElement.name,
            NostrTagElement.description,
            uses,
        )
        .join(
            NostrUserTagging,
            NostrUserTagging.tag_event_id == NostrTagElement.event_id,
        )
        .where(NostrUserTagging.asserter_pubkey.in_(qualifying_asserters))
        .where(
            (NostrUserTagging.polarity >= APPLY_THRESHOLD)
            | (NostrUserTagging.polarity <= DISPUTE_THRESHOLD)
        )
        .group_by(
            NostrTagElement.event_id,
            NostrTagElement.author_pubkey,
            NostrTagElement.slug,
            NostrTagElement.name,
            NostrTagElement.description,
        )
        .having(uses >= min_uses)
        .order_by(uses.desc(), NostrTagElement.event_id.asc())
    )
    result = await db.execute(stmt)
    return [
        DictionaryEntry(
            tag_event_id=row[0],
            tag_author_pubkey=row[1],
            slug=row[2],
            name=row[3],
            description=row[4],
            uses=int(row[5]),
        )
        for row in result.all()
    ]


async def get_taggings_for_tag_on_db(
    db: AsyncDBSession, tag_event_id: str, qualifying_asserters: list[str]
) -> list[tuple[str, float, str]]:
    """(target_pubkey, polarity, asserter_pubkey) for one tag, restricted to
    qualifying asserters.

    One row per live assertion — replaceability already collapsed them at write
    time, so the caller can bucket without deduping. The asserter rides along so
    the weighted fold can look up that asserter's trust weight (D12).
    """
    if not qualifying_asserters:
        return []
    stmt = (
        select(
            NostrUserTagging.target_pubkey,
            NostrUserTagging.polarity,
            NostrUserTagging.asserter_pubkey,
        )
        .where(NostrUserTagging.tag_event_id == tag_event_id)
        .where(NostrUserTagging.asserter_pubkey.in_(qualifying_asserters))
    )
    result = await db.execute(stmt)
    return [(row[0], float(row[1]), row[2]) for row in result.all()]
