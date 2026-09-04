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

import json
from dataclasses import dataclass, field
from datetime import timedelta

from nostr_sdk import (  # type: ignore
    Client,
    EventBuilder,
    Filter,
    Keys,
    Kind,
    NostrSigner,
    PublicKey,
    Tag,
)

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
from sqlalchemy.ext.asyncio import AsyncSession as AsyncDBSession

from app.repos.brainstorm_nsec import get_graperank_preset_by_pubkey_on_db
from app.repos.user_repo import get_qualifying_asserters_for_observer
from app.services.graperank_preset_service import (
    normalize_preset,
    resolve_preset_params,
)
from app.services.trusted_list_build import (
    D_TAG_PREFIX,
    DEFAULT_RIGOR,
    TRUSTED_LIST_KIND,
    build_trusted_list_content,
    build_trusted_list_tags,
    compute_d_tag,
    compute_members,
)

logger = loggr.get_logger(__name__)

# Why a run produced no lists. AC15: an operator must be able to tell an
# un-synced relay from a quiet day, because the two look identical downstream.
# An Observer holds tens of TLs, so one bounded fetch covers the whole set.
RETRACTION_SCAN_LIMIT = 500

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


async def _fetch_published_tl_slots(client: Client, signing_pubkey: str) -> dict:
    """This Observer's live kind-30392 slots, as {d_tag: already_retracted}.

    Scoped by author to the Observer's own assistant key, so a run for X can
    never see — let alone retract — Y's lists. Filtered again on the `tl-tag-`
    prefix so a slot published by some other derivation (tapestry's pin-derived
    `tl-pin-` lists, if a relay mirrors both) is never touched.

    Unlike the taggings read, REQ recall is not a concern here: an Observer has
    tens of TLs, far below strfry's maxFilterLimit.
    """
    flt = (
        Filter()
        .kinds([Kind(TRUSTED_LIST_KIND)])
        .authors([PublicKey.parse(signing_pubkey)])
        .limit(RETRACTION_SCAN_LIMIT)
    )
    events = await client.fetch_events(flt, timeout=timedelta(seconds=30))
    slots: dict[str, bool] = {}
    for event in events.to_vec():
        d_tag = None
        retracted = False
        for tag in json.loads(event.as_json()).get("tags", []):
            if not tag:
                continue
            if tag[0] == "d" and len(tag) >= 2:
                d_tag = tag[1]
            elif tag[0] == "status" and len(tag) >= 2 and tag[1] == "retracted":
                retracted = True
        if d_tag and d_tag.startswith(f"{D_TAG_PREFIX}-"):
            # Keep the most pessimistic view across duplicates: if any copy is
            # unretracted we still owe a retraction.
            slots[d_tag] = slots.get(d_tag, True) and retracted
    return slots


def _parse_d_tag_slug(d_tag: str, observer: str) -> tuple[str, str] | None:
    """(tag_author8, slug) from `tl-tag-<observer8>-<tagAuthor8>-<slug>`.

    Slugs may contain `-`, so split only the fixed leading fields. Returns None
    for a slot belonging to a different Observer.
    """
    prefix = f"{D_TAG_PREFIX}-{observer[:8]}-"
    if not d_tag.startswith(prefix):
        return None
    rest = d_tag.removeprefix(prefix)
    author8, _, slug = rest.partition("-")
    if not author8 or not slug:
        return None
    return author8, slug


async def _resolve_rigor(db: AsyncDBSession, observer_pubkey: str) -> float:
    """This Observer's GrapeRank rigor, off their saved preset.

    Reuses the same resolver the GrapeRank request path uses
    (`brainstorm_request_service`), so a TL run and a scorecard run can never
    disagree about which parameters this Observer is on: unset resolves to
    DEFAULT, CUSTOM with no stored params falls back to DEFAULT with a warning.

    Deliberate divergence from tapestry, which hardcodes 0.5 (ADR D12). Both
    estates publish the value on the event, so a consumer reproduces the score
    either way; what changes is that a PERMISSIVE Observer (0.3) reaches
    confidence on less trust mass than a RESTRICTIVE one (0.65). DEFAULT is
    seeded at 0.5, so an unconfigured Observer still matches tapestry exactly.

    Never fatal: rigor is a refinement, and failing a whole publish run over an
    unreadable preset would be a worse outcome than scoring at the default.
    """
    try:
        stored = await get_graperank_preset_by_pubkey_on_db(db, observer_pubkey)
        _effective, params = await resolve_preset_params(
            db, normalize_preset(stored), pubkey=observer_pubkey
        )
        rigor = float(params.rigor)
    except Exception:
        logger.exception(
            "Could not resolve GrapeRank preset for observer %s; "
            "scoring Trusted Lists at the default rigor %s",
            observer_pubkey,
            DEFAULT_RIGOR,
        )
        return DEFAULT_RIGOR

    if not 0.0 <= rigor < 1.0:
        # rigor >= 1 makes certainty identically 0, which silently empties
        # every list this Observer has. Refuse it rather than publish nothing.
        logger.error(
            "Observer %s has rigor=%s, which is outside [0, 1) and would empty "
            "every Trusted List; scoring at the default %s instead",
            observer_pubkey,
            rigor,
            DEFAULT_RIGOR,
        )
        return DEFAULT_RIGOR
    return rigor


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
        rigor = await _resolve_rigor(db, observer_pubkey)

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

    # `qualifying` maps asserter -> trust weight; the repos only need the keys.
    qualifying_pubkeys = list(qualifying)
    async with db_session() as db:
        dictionary = await get_dictionary_on_db(
            db,
            qualifying_asserters=qualifying_pubkeys,
            min_uses=settings.trusted_list_min_tag_uses,
        )
        per_tag_taggings = {
            entry.tag_event_id: await get_taggings_for_tag_on_db(
                db,
                tag_event_id=entry.tag_event_id,
                qualifying_asserters=qualifying_pubkeys,
            )
            for entry in dictionary
        }

    result.dictionary_size = len(dictionary)
    if not dictionary:
        # NOTE: deliberately does NOT return early. An empty dictionary reached
        # from a TRUSTWORTHY view — we hold taggings AND this Observer has
        # qualifying asserters — means every tag legitimately fell out, and its
        # stale lists must be retracted. That is the commonest retraction case.
        #
        # The two earlier returns above are the untrustworthy views (nothing
        # ingested; Observer never scored). Those must never reach the
        # retraction pass: an empty result caused by a broken relay sync or an
        # unscored Observer would otherwise wipe every live list this Observer
        # has. Emptiness is only actionable when we know why it is empty.
        result.empty_reason = EMPTY_REASON_NO_TAGS_MET_THRESHOLD

    # --- write phase: per-tag failures are isolated -------------------------
    client = await _connect(nsec)
    current_d_tags: set[str] = set()
    cutoff = settings.trusted_list_cutoff

    for entry in dictionary:
        taggings = per_tag_taggings.get(entry.tag_event_id, [])
        # Attach each asserter's trust weight. The repo restricted the rows to
        # qualifying asserters, so every lookup hits; 0.0 is a defensive floor
        # that scores the tagging out rather than crashing the run.
        weighted = [
            (target, polarity, qualifying.get(asserter, 0.0))
            for target, polarity, asserter in taggings
        ]
        members = compute_members(weighted, cutoff=cutoff, rigor=rigor)
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
                    rigor=rigor,
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

    # --- retraction pass (AC13) --------------------------------------------
    # `current_d_tags` includes tags whose publish FAILED. That inclusion is the
    # safety rule (AC14): a transient relay error must never let this pass empty
    # a healthy live list. Scoped to this Observer's own signing key, so a run
    # for X cannot touch Y's slots.
    try:
        published_slots = await _fetch_published_tl_slots(client, result.signing_pubkey)
    except Exception as exc:  # noqa: BLE001
        # Can't enumerate what's live -> retract nothing. Publishing above
        # already succeeded; skipping retraction leaves stale lists in place,
        # which is strictly safer than guessing and wiping good ones.
        logger.error(
            "TL retraction scan failed for observer %s (retracting nothing): %s",
            observer_pubkey,
            exc,
        )
        return result

    for stale_d_tag in plan_retractions(list(published_slots), current_d_tags):
        if published_slots.get(stale_d_tag):
            continue  # already carries the retracted marker — idempotent
        parsed = _parse_d_tag_slug(stale_d_tag, observer_pubkey)
        if parsed is None:
            continue
        author8, slug = parsed
        try:
            await _publish(
                client,
                build_trusted_list_tags(
                    observer=observer_pubkey,
                    tag_event_id="",
                    tag_author_pubkey=author8,
                    slug=slug,
                    name=slug,
                    description="",
                    members=[],
                    cutoff=cutoff,
                    min_rank=settings.trusted_list_min_rank,
                    retracted=True,
                ),
                "",
            )
            result.retracted += 1
            result.tags.append(
                TagResult(
                    slug=slug,
                    d_tag=stale_d_tag,
                    tag_event_id="",
                    status="retracted",
                )
            )
        except Exception as exc:  # noqa: BLE001
            logger.error("TL retraction failed for %s: %s", stale_d_tag, exc)

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
