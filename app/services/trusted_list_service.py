"""Trusted List generation for one Observer.

The pipeline, per ADR `trusted-lists/0001` (people) and `0002` (content):

    taggings (Postgres): pubkey taggings, and event taggings via their headers
      -> qualifying asserters (Neo4j, this Observer's web of trust)
      -> dictionary: Tags used >= min_uses (30392), and (Tag, tagging target)
         pairs used >= min_uses (30393 / 30394), the targets worked out by
         lookups (`tagging_target_service`)
      -> per entry: membership (applications >= cutoff and score >= 1)
      -> signed by the Observer's assistant nsec, published
      -> stale slots retracted, per kind, from a trusted view only

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
from sqlalchemy.ext.asyncio import AsyncSession as AsyncDBSession

from app.core.config import settings
from app.core.database import db_session
from app.core.loggr import loggr
from app.neo4j_db.driver import driver as neo4j_driver
from app.repos.brainstorm_nsec import (
    get_graperank_preset_by_pubkey_on_db,
    get_or_create_brainstorm_observer_nsec_by_pubkey_on_db,
)
from app.repos.event_tagging_repo import (
    EventTaggingRow,
    TaggedEvent,
    count_event_taggings_on_db,
    get_event_tagging_asserters_on_db,
    get_event_taggings_on_db,
    get_tagged_events_on_db,
    insert_tagged_events_on_db,
)
from app.repos.tagging_repo import (
    count_taggings_on_db,
    get_asserter_pubkeys_on_db,
    get_dictionary_on_db,
    get_taggings_for_tag_on_db,
)
from app.repos.user_repo import get_qualifying_asserters_for_observer
from app.services.graperank_preset_service import (
    normalize_preset,
    resolve_preset_params,
)
from app.services.tagging_parse import parse_coordinate
from app.services.tagging_target_service import (
    DLIST_HEADER_ID_KIND,
    DLIST_ITEM_ID_KIND,
    DLIST_ITEM_KIND,
    Lookup,
    LookupFailed,
    coordinate_of,
    dlist_name,
    header_refs,
    resolve_target,
)
from app.services.trusted_list_build import (
    CONTENT_LIST_KINDS,
    D_TAG_PREFIX,
    DEFAULT_RIGOR,
    KIND_NAMES,
    TRUSTED_LIST_KIND,
    ContentStance,
    Member,
    build_content_list_content,
    build_content_list_tags,
    build_trusted_list_content,
    build_trusted_list_tags,
    compute_content_d_tag,
    compute_d_tag,
    compute_members,
    content_d_kind_prefix,
    count_uses,
    one_stance_per_asserter,
)
from app.utils.client_tag import client_tag

logger = loggr.get_logger(__name__)

# Why a run produced no lists. AC15: an operator must be able to tell an
# un-synced relay from a quiet day, because the two look identical downstream.
# An Observer holds tens of TLs, so one bounded fetch covers the whole set.
RETRACTION_SCAN_LIMIT = 500

EMPTY_REASON_NO_TAGGINGS = "no_taggings_ingested"
EMPTY_REASON_NO_QUALIFYING_ASSERTERS = "no_qualifying_asserters"
EMPTY_REASON_NO_TAGS_MET_THRESHOLD = "no_tags_met_use_threshold"

# Why a run published and retracted no content list (ADR 0002 D8).
CONTENT_SKIPPED_LOOKUP_FAILED = "target_lookup_failed"


@dataclass
class TagResult:
    slug: str
    d_tag: str
    tag_event_id: str
    status: str  # "published" | "retracted" | "failed"
    taggings_considered: int = 0
    member_count: int = 0
    error: str | None = None
    # 30392 (people), 30393 (events by id) or 30394 (by coordinate).
    kind: int = TRUSTED_LIST_KIND
    # A content list's `tagging-target`: an event kind, or a DList's header.
    tagging_target: str | None = None


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
    event_taggings_in_store: int = 0
    # Distinct targets no lookup found this run; retried next run (AC7).
    unresolved_targets: int = 0
    # Kinds that were tagged but have no name in KIND_NAMES, so got no list.
    unnamed_kinds: list[int] = field(default_factory=list)
    # Set when no content list was published or retracted, and why (D8).
    content_skipped_reason: str | None = None


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


async def _publish(
    client: Client,
    tags: list[list[str]],
    content: str,
    kind: int = TRUSTED_LIST_KIND,
) -> None:
    builder = EventBuilder(kind=Kind(kind), content=content).tags(
        [Tag.parse(t) for t in tags if t[0] != "client"] + [client_tag()]
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


@dataclass(frozen=True)
class ContentSlot:
    """A live 30393 / 30394 this signer published, and the identity tags a
    retraction copies off it (ADR 0002 D8): the `d` can't be split reliably,
    since slugs and DList `d`s both hold `-`."""

    kind: int
    d_tag: str
    retracted: bool
    title: str | None
    source_tag: list[str] | None
    tagging_target: str | None

    @property
    def readable(self) -> bool:
        """Whether a retraction can carry this slot's identity. A slot that
        can't is left in place rather than retracted under a guessed one."""
        if not self.title or not self.tagging_target or not self.source_tag:
            return False
        if len(self.source_tag) < 4:
            return False
        coordinate = parse_coordinate(self.source_tag[1])
        return coordinate is not None and coordinate.kind == 39999


async def _fetch_published_content_slots(
    client: Client, signing_pubkey: str
) -> list[ContentSlot]:
    """This signer's live 30393 and 30394 slots, newest per (kind, `d`).

    Scoped by author, and to the `tl-tag-notes-` / `tl-tag-items-` prefixes,
    like `_fetch_published_tl_slots`. A duplicate copy that is unretracted wins
    over a retracted one: we still owe it a retraction.
    """
    flt = (
        Filter()
        .kinds([Kind(k) for k in CONTENT_LIST_KINDS])
        .authors([PublicKey.parse(signing_pubkey)])
        .limit(RETRACTION_SCAN_LIMIT)
    )
    events = await client.fetch_events(flt, timeout=timedelta(seconds=30))
    slots: dict[tuple[int, str], ContentSlot] = {}
    for event in events.to_vec():
        slot = content_slot_of(json.loads(event.as_json()))
        if slot is None:
            continue
        key = (slot.kind, slot.d_tag)
        held = slots.get(key)
        if held is None or (held.retracted and not slot.retracted):
            slots[key] = slot
    return list(slots.values())


def content_slot_of(event: dict) -> ContentSlot | None:
    """A live content slot off its event, or None if it isn't one of ours."""
    kind = event.get("kind")
    if kind not in CONTENT_LIST_KINDS:
        return None
    first: dict[str, list] = {}
    retracted = False
    for tag in event.get("tags") or []:
        if not isinstance(tag, list) or not tag or not isinstance(tag[0], str):
            continue
        if tag[0] == "status" and tag[1:2] == ["retracted"]:
            retracted = True
        first.setdefault(tag[0], tag)
    d_tag = first.get("d", [None, None])[1:2]
    if not d_tag or not isinstance(d_tag[0], str):
        return None
    if not d_tag[0].startswith(content_d_kind_prefix(kind)):
        return None

    def value(name: str) -> str | None:
        tag = first.get(name)
        v = tag[1] if tag is not None and len(tag) >= 2 else None
        return v if isinstance(v, str) and v else None

    source_tag = first.get("source-tag")
    return ContentSlot(
        kind=kind,
        d_tag=d_tag[0],
        retracted=retracted,
        title=value("title"),
        source_tag=[str(v) for v in source_tag] if source_tag is not None else None,
        tagging_target=value("tagging-target"),
    )


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


@dataclass
class TargetView:
    """What this run's lookups learned (ADR 0002 D6): events by id (cached or
    fresh), 39999 items by coordinate (fresh), DList headers by coordinate
    (fresh)."""

    by_id: dict[str, TaggedEvent] = field(default_factory=dict)
    by_coordinate: dict[str, TaggedEvent] = field(default_factory=dict)
    headers: dict[str, TaggedEvent] = field(default_factory=dict)


async def _ids_cached_or_fetched(
    ids: set[str], lookup: Lookup, hints: set[str]
) -> dict[str, TaggedEvent]:
    """Events by id: the cache first, then a lookup for the rest. What the
    lookup finds is cached; what it doesn't is looked up again next run."""
    if not ids:
        return {}
    async with db_session() as db:
        found = await get_tagged_events_on_db(db, sorted(ids))
    missing = ids - set(found)
    if missing:
        fresh = await lookup.by_ids(missing, hints)
        if fresh:
            async with db_session() as db:
                await insert_tagged_events_on_db(db, list(fresh.values()))
        found.update(fresh)
    return found


async def gather_target_view(rows: list[EventTaggingRow], lookup: Lookup) -> TargetView:
    """Every lookup the rows need, in three rounds: the ids, then the 39999
    items by coordinate, then their DList headers. Raises `LookupFailed`."""
    hints = {r.target_relay for r in rows if r.target_relay}
    view = TargetView()
    view.by_id = await _ids_cached_or_fetched(
        {r.target for r in rows if r.target_slot == "e"}, lookup, hints
    )

    items: set[str] = set()
    for row in rows:
        coordinate = parse_coordinate(row.target) if row.target_slot == "a" else None
        if coordinate is not None and coordinate.kind == DLIST_ITEM_KIND:
            items.add(str(coordinate))
    for facts in view.by_id.values():
        if facts.kind == DLIST_ITEM_KIND:
            items.add(coordinate_of(facts))
    view.by_coordinate = await lookup.by_coordinates(items, hints)

    refs: set[str] = set()
    for facts in [*view.by_coordinate.values(), *view.by_id.values()]:
        if facts.kind in (DLIST_ITEM_KIND, DLIST_ITEM_ID_KIND):
            refs.update(header_refs(facts))
    header_coordinates = {r for r in refs if parse_coordinate(r) is not None}
    view.headers = await lookup.by_coordinates(header_coordinates, hints)
    view.by_id.update(
        await _ids_cached_or_fetched(
            refs - header_coordinates - set(view.by_id), lookup, hints
        )
    )
    return view


@dataclass
class ContentList:
    """One planned 30393 / 30394: an (Observer, Tag, tagging target) pair."""

    list_kind: int
    d_tag: str
    title: str
    description: str
    tag_event_id: str
    tag_author_pubkey: str
    slug: str
    tagging_target: str
    taggings_considered: int
    members: list[Member]


@dataclass
class ContentPlan:
    lists: list[ContentList] = field(default_factory=list)
    unresolved_targets: int = 0
    unnamed_kinds: list[int] = field(default_factory=list)


def plan_content_lists(
    *,
    observer: str,
    rows: list[EventTaggingRow],
    view: TargetView,
    weights: dict[str, float],
    cutoff: int,
    rigor: float,
    min_uses: int,
) -> ContentPlan:
    """The content dictionary and each pair's members (ADR 0002 D1-D4, D7).

    Pure: every fact it needs is in `view`. A target the lookups didn't find,
    or whose DList can't be named, is counted unresolved and joins no list; a
    kind with no name is reported and gets no list.
    """
    unresolved: set[str] = set()
    unnamed: set[int] = set()
    groups: dict[tuple[str, int, str], tuple[EventTaggingRow, list[ContentStance]]] = {}

    for row in rows:
        resolution = resolve_target(
            row.target, row.target_slot, view.by_id, view.by_coordinate
        )
        if resolution is None:
            unresolved.add(row.target)
            continue
        for target in resolution.targets:
            if target.dlist is not None and parse_coordinate(target.dlist) is None:
                header = view.by_id.get(target.dlist)
                if header is None:
                    unresolved.add(row.target)
                    continue
                if header.kind != DLIST_HEADER_ID_KIND:
                    continue  # an id in `z` that isn't a DList header
            key = (row.tag_event_id, resolution.list_kind, target.value)
            _, stances = groups.setdefault(key, (row, []))
            stances.append(
                ContentStance(
                    asserter=row.asserter_pubkey,
                    member=resolution.member,
                    polarity=row.polarity,
                    created_at_unix=row.created_at_unix,
                )
            )

    plan = ContentPlan()
    for (tag_event_id, list_kind, target_value), (row, raw) in sorted(groups.items()):
        stances = one_stance_per_asserter(raw)
        if count_uses(stances) < min_uses:
            continue
        if parse_coordinate(target_value) is None and target_value.isdigit():
            kind = int(target_value)
            target_name = KIND_NAMES.get(kind)
            if target_name is None:
                unnamed.add(kind)
                continue
            d_tag = compute_content_d_tag(
                list_kind, observer, row.tag_author_pubkey, row.slug, kind=kind
            )
        else:
            target_name = dlist_name(target_value, view.headers, view.by_id)
            if target_name is None:
                unresolved.update(s.member for s in stances)
                continue
            d_tag = compute_content_d_tag(
                list_kind, observer, row.tag_author_pubkey, row.slug, dlist=target_value
            )
        members = compute_members(
            [(s.member, s.polarity, weights.get(s.asserter, 0.0)) for s in stances],
            cutoff=cutoff,
            rigor=rigor,
        )
        plan.lists.append(
            ContentList(
                list_kind=list_kind,
                d_tag=d_tag,
                title=f"{row.name} {target_name}",
                description=row.description,
                tag_event_id=tag_event_id,
                tag_author_pubkey=row.tag_author_pubkey,
                slug=row.slug,
                tagging_target=target_value,
                taggings_considered=len(stances),
                members=members,
            )
        )
    plan.unresolved_targets = len(unresolved)
    plan.unnamed_kinds = sorted(unnamed)
    return plan


async def generate_trusted_lists_for_observer(
    observer_pubkey: str,
    lookup: Lookup | None = None,
) -> TrustedListRunResult:
    """Compute and publish this Observer's Trusted Lists, all three kinds, in
    one run (ADR 0002 D8). Admin-triggered."""
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
        result.event_taggings_in_store = await count_event_taggings_on_db(db)
        asserters = await get_asserter_pubkeys_on_db(db)
        if result.event_taggings_in_store:
            asserters = sorted(
                set(asserters) | set(await get_event_tagging_asserters_on_db(db))
            )
        rigor = await _resolve_rigor(db, observer_pubkey)

    result.signing_pubkey = Keys.parse(secret_key=nsec).public_key().to_hex()

    if result.taggings_in_store == 0 and result.event_taggings_in_store == 0:
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
    dictionary = []
    per_tag_taggings: dict[str, list[tuple[str, float, str]]] = {}
    event_rows: list[EventTaggingRow] = []
    async with db_session() as db:
        if result.taggings_in_store:
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
        if result.event_taggings_in_store:
            event_rows = await get_event_taggings_on_db(db, qualifying_pubkeys)

    # Each kind's view is trusted or not on its own (S1). People lists: only
    # when pubkey taggings are held (story 1's rule). Content lists: only when
    # event taggings are held AND every configured lookup relay answered (D8).
    people_trusted = result.taggings_in_store > 0
    content_trusted = result.event_taggings_in_store > 0
    cutoff = settings.trusted_list_cutoff
    content = ContentPlan()
    if content_trusted:
        try:
            view = await gather_target_view(event_rows, lookup or Lookup())
        except LookupFailed as exc:
            logger.error(
                "Target lookup failed for observer %s; publishing and retracting "
                "no content list this run: %s",
                observer_pubkey,
                exc,
            )
            content_trusted = False
            result.content_skipped_reason = f"{CONTENT_SKIPPED_LOOKUP_FAILED}: {exc}"
        else:
            content = plan_content_lists(
                observer=observer_pubkey,
                rows=event_rows,
                view=view,
                weights=qualifying,
                cutoff=cutoff,
                rigor=rigor,
                min_uses=settings.trusted_list_min_tag_uses,
            )
            result.unresolved_targets = content.unresolved_targets
            result.unnamed_kinds = content.unnamed_kinds

    result.dictionary_size = len(dictionary) + len(content.lists)
    if not dictionary and not content.lists:
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

    # --- write phase: per-list failures are isolated ------------------------
    client = await _connect(nsec)
    current_d_tags: set[str] = set()

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

    current_content: set[tuple[int, str]] = set()
    for planned in content.lists:
        # Current before the publish, for the same reason as above (AC15).
        current_content.add((planned.list_kind, planned.d_tag))
        tag_result = TagResult(
            slug=planned.slug,
            d_tag=planned.d_tag,
            tag_event_id=planned.tag_event_id,
            status="published",
            taggings_considered=planned.taggings_considered,
            member_count=len(planned.members),
            kind=planned.list_kind,
            tagging_target=planned.tagging_target,
        )
        try:
            await _publish(
                client,
                build_content_list_tags(
                    list_kind=planned.list_kind,
                    d_tag=planned.d_tag,
                    observer=observer_pubkey,
                    title=planned.title,
                    description=planned.description,
                    tag_author_pubkey=planned.tag_author_pubkey,
                    slug=planned.slug,
                    tagging_target=planned.tagging_target,
                    members=planned.members,
                    cutoff=cutoff,
                    min_rank=settings.trusted_list_min_rank,
                    rigor=rigor,
                ),
                build_content_list_content(planned.list_kind, planned.members),
                kind=planned.list_kind,
            )
            result.published += 1
        except Exception as exc:  # noqa: BLE001 — per-list isolation
            logger.error(
                "TL publish failed for observer %s list %s: %s",
                observer_pubkey,
                planned.d_tag,
                exc,
            )
            tag_result.status = "failed"
            tag_result.error = str(exc)
            result.failed += 1
        result.tags.append(tag_result)

    # --- retraction pass (AC13; ADR 0002 D8) --------------------------------
    # The current sets include lists whose publish FAILED. That inclusion is
    # the safety rule (AC14): a transient relay error must never let this pass
    # empty a healthy live list. Scoped to this Observer's own signing key, so
    # a run for X cannot touch Y's slots. One read covers all three kinds: if
    # any of it fails, nothing of any kind is retracted.
    try:
        published_slots = (
            await _fetch_published_tl_slots(client, result.signing_pubkey)
            if people_trusted
            else {}
        )
        content_slots = (
            await _fetch_published_content_slots(client, result.signing_pubkey)
            if content_trusted
            else []
        )
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

    for slot in plan_content_retractions(content_slots, current_content):
        if not slot.readable:
            logger.warning(
                "Live kind-%s slot %s for observer %s lacks readable identity tags; "
                "leaving it in place",
                slot.kind,
                slot.d_tag,
                observer_pubkey,
            )
            continue
        source_tag = slot.source_tag or []
        try:
            await _publish(
                client,
                build_content_list_tags(
                    list_kind=slot.kind,
                    d_tag=slot.d_tag,
                    observer=observer_pubkey,
                    title=slot.title or "",
                    description="",
                    tag_author_pubkey=source_tag[2],
                    slug=source_tag[3],
                    tagging_target=slot.tagging_target or "",
                    members=[],
                    cutoff=cutoff,
                    min_rank=settings.trusted_list_min_rank,
                    retracted=True,
                ),
                "",
                kind=slot.kind,
            )
            result.retracted += 1
            result.tags.append(
                TagResult(
                    slug=source_tag[3],
                    d_tag=slot.d_tag,
                    tag_event_id="",
                    status="retracted",
                    kind=slot.kind,
                    tagging_target=slot.tagging_target,
                )
            )
        except Exception as exc:  # noqa: BLE001
            logger.error("TL retraction failed for %s: %s", slot.d_tag, exc)

    return result


def plan_content_retractions(
    slots: list[ContentSlot], current: set[tuple[int, str]]
) -> list[ContentSlot]:
    """Live content slots to retract: not current this run (a failed publish
    counts as current), and not already retracted. Readability is checked by
    the caller, which leaves an unreadable slot in place."""
    return [
        slot
        for slot in slots
        if (slot.kind, slot.d_tag) not in current and not slot.retracted
    ]


def plan_retractions(
    published_d_tags: list[str], current_d_tags: set[str]
) -> list[str]:
    """Slots to retract: previously published, no longer in the dictionary.

    Pure so the "a failed publish keeps its slot" rule is testable without a
    relay. Callers pass the CURRENT set including failed tags — that inclusion
    is what makes a transient failure non-destructive.
    """
    return [d for d in published_d_tags if d not in current_d_tags]
