"""Trusted List generation for one Observer.

The pipeline, per ADR `trusted-lists/0001`:

    taggings (Postgres)
      -> qualifying asserters (Neo4j, this Observer's web of trust)
      -> dictionary (tags used >= min_uses by a qualifying asserter)
      -> per tag: membership (applications >= cutoff and > disputes)
      -> kind-30392 signed by the Observer's assistant nsec, published
      -> stale slots retracted

Failure policy differs by dependency and the difference is deliberate:
Postgres and Neo4j failures abort the whole run BEFORE anything is published,
because a silently empty dictionary does not publish nothing — it publishes
signed claims that people belong to nothing. Relay failures are per-tag and
never abort the run, and a tag whose publish failed keeps its slot marked
current so the retraction pass cannot wipe the healthy list still on the relay.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import timedelta

from nostr_sdk import Client, EventBuilder, Keys, Kind, NostrSigner, Tag  # type: ignore

from app.core.config import settings
from app.core.database import db_session
from app.core.loggr import loggr
from app.neo4j_db.driver import driver as neo4j_driver
from app.repos.brainstorm_nsec import (
    get_or_create_brainstorm_observer_nsec_by_pubkey_on_db,
)
from app.repos.tagging_repo import (
    count_taggings_on_db,
    get_asserter_pubkeys_on_db,
    get_dictionary_on_db,
    get_taggings_for_tag_on_db,
)
from app.repos.user_repo import get_qualifying_asserters_for_observer
from app.services.trusted_list_build import (
    TRUSTED_LIST_KIND,
    build_trusted_list_content,
    build_trusted_list_tags,
    compute_d_tag,
    compute_members,
)

logger = loggr.get_logger(__name__)

# Why a run produced no lists. AC15: an operator must be able to tell an
# un-synced relay from a quiet day, because the two look identical downstream.
EMPTY_REASON_NO_TAGGINGS = "no_taggings_ingested"
EMPTY_REASON_NO_QUALIFYING_ASSERTERS = "no_qualifying_asserters"
EMPTY_REASON_NO_TAGS_MET_THRESHOLD = "no_tags_met_use_threshold"


@dataclass
class TagResult:
    slug: str
    d_tag: str
    tag_event_id: str
    status: str  # "published" | "retracted" | "failed"
    taggings_considered: int = 0
    member_count: int = 0
    error: str | None = None


@dataclass
class TrustedListRunResult:
    observer: str
    signing_pubkey: str | None = None
    taggings_in_store: int = 0
    qualifying_asserters: int = 0
    dictionary_size: int = 0
    published: int = 0
    failed: int = 0
    retracted: int = 0
    empty_reason: str | None = None
    tags: list[TagResult] = field(default_factory=list)


def _relay_url() -> str:
    return settings.trusted_list_relay or settings.nostr_upload_ta_events_relay


async def _connect(nsec: str) -> Client:
    keys = Keys.parse(secret_key=nsec)
    client = Client(signer=NostrSigner.keys(keys=keys))
    await client.add_relay(_relay_url())
    result = await client.try_connect(timedelta(seconds=10))
    if bool(result.failed):
        raise RuntimeError(f"could not connect to {_relay_url()}")
    return client


async def _publish(client: Client, tags: list[list[str]], content: str) -> None:
    builder = EventBuilder(kind=Kind(TRUSTED_LIST_KIND), content=content).tags(
        [Tag.parse(t) for t in tags]
    )
    event = await client.sign_event_builder(builder)
    output = await client.send_event(event)
    if output.failed:
        raise RuntimeError(str(output.failed))


async def generate_trusted_lists_for_observer(
    observer_pubkey: str,
) -> TrustedListRunResult:
    """Compute and publish this Observer's Trusted Lists. Admin-triggered."""
    result = TrustedListRunResult(observer=observer_pubkey)

    # --- read phase: any failure here raises before we publish anything -----
    async with db_session() as db:
        (
            nsec_row,
            _created,
        ) = await get_or_create_brainstorm_observer_nsec_by_pubkey_on_db(
            db, pubkey=observer_pubkey
        )
        nsec = nsec_row.nsec
        result.taggings_in_store = await count_taggings_on_db(db)
        asserters = await get_asserter_pubkeys_on_db(db)

    result.signing_pubkey = Keys.parse(secret_key=nsec).public_key().to_hex()

    if result.taggings_in_store == 0:
        # Nothing ingested at all. Distinct from "nobody qualified" — this is
        # the shape an un-synced relay takes, and it must not read as a quiet day.
        result.empty_reason = EMPTY_REASON_NO_TAGGINGS
        return result

    min_influence = settings.trusted_list_min_rank / 100.0
    async with neo4j_driver.session() as neo_session:
        qualifying = await get_qualifying_asserters_for_observer(
            neo_session,
            pubkeys=asserters,
            observer_pubkey=observer_pubkey,
            min_influence=min_influence,
        )
    result.qualifying_asserters = len(qualifying)
    if not qualifying:
        result.empty_reason = EMPTY_REASON_NO_QUALIFYING_ASSERTERS
        return result

    async with db_session() as db:
        dictionary = await get_dictionary_on_db(
            db,
            qualifying_asserters=qualifying,
            min_uses=settings.trusted_list_min_tag_uses,
        )
        per_tag_taggings = {
            entry.tag_event_id: await get_taggings_for_tag_on_db(
                db, tag_event_id=entry.tag_event_id, qualifying_asserters=qualifying
            )
            for entry in dictionary
        }

    result.dictionary_size = len(dictionary)
    if not dictionary:
        result.empty_reason = EMPTY_REASON_NO_TAGS_MET_THRESHOLD
        return result

    # --- write phase: per-tag failures are isolated -------------------------
    client = await _connect(nsec)
    current_d_tags: set[str] = set()
    cutoff = settings.trusted_list_cutoff

    for entry in dictionary:
        taggings = per_tag_taggings.get(entry.tag_event_id, [])
        members = compute_members(taggings, cutoff=cutoff)
        d_tag = compute_d_tag(observer_pubkey, entry.tag_author_pubkey, entry.slug)
        # Marked current BEFORE the publish is attempted: a transient relay
        # failure must not let the retraction pass empty a live list.
        current_d_tags.add(d_tag)

        tag_result = TagResult(
            slug=entry.slug,
            d_tag=d_tag,
            tag_event_id=entry.tag_event_id,
            status="published",
            taggings_considered=len(taggings),
            member_count=len(members),
        )
        try:
            await _publish(
                client,
                build_trusted_list_tags(
                    observer=observer_pubkey,
                    tag_event_id=entry.tag_event_id,
                    tag_author_pubkey=entry.tag_author_pubkey,
                    slug=entry.slug,
                    name=entry.name,
                    description=entry.description,
                    members=members,
                    cutoff=cutoff,
                    min_rank=settings.trusted_list_min_rank,
                ),
                build_trusted_list_content(members),
            )
            result.published += 1
        except Exception as exc:  # noqa: BLE001 — per-tag isolation is the point
            logger.error(
                "TL publish failed for observer %s tag %s: %s",
                observer_pubkey,
                entry.slug,
                exc,
            )
            tag_result.status = "failed"
            tag_result.error = str(exc)
            result.failed += 1
        result.tags.append(tag_result)

    return result


def plan_retractions(
    published_d_tags: list[str], current_d_tags: set[str]
) -> list[str]:
    """Slots to retract: previously published, no longer in the dictionary.

    Pure so the "a failed publish keeps its slot" rule is testable without a
    relay. Callers pass the CURRENT set including failed tags — that inclusion
    is what makes a transient failure non-destructive.
    """
    return [d for d in published_d_tags if d not in current_d_tags]
