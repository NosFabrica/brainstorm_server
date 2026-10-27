import time
from collections.abc import Awaitable, Callable, Iterable
from datetime import datetime, timedelta
from itertools import islice

from app.core.config import settings
from app.core.database import db_session
from app.core.loggr import loggr
from app.db_models import BrainstormRequestStatus
from app.models.grapeRankResult import GrapeRankResult, ScoreCard
from app.neo4j_db.driver import driver as neo4j_driver
from app.repos.brainstorm_nsec import (
    get_last_neo4j_cleanup_at_on_db,
    mark_neo4j_cleanup_on_db,
)
from app.repos.brainstorm_request_repo import (
    select_brainstorm_request_by_id_on_db,
    update_brainstorm_request_internal_publication_status_by_id_on_db,
)
from app.repos.user_repo import (
    clear_observer_scores,
    get_pubkeys_scored_by_observer,
    set_observer_scores,
)
from app.utils.datetimes import utc_now

# Each batch is its own transaction, so fewer, larger batches mean fewer commits.
BATCH_SIZE = 5000

# Persisted per observer as `<field>_<observer_pubkey>`. `trusted_followers` is
# here so /networkAlerts reads a property instead of scanning follower edges.
# Cast: a pre-cutover worker sends hops/counts as floats; Neo4j keeps the type it gets.
PERSISTED_FIELDS = {"influence": float, "hops": int, "trusted_followers": int}

logger = loggr.get_logger(__name__)


def _keys(observer: str) -> dict[str, str]:
    return {field: f"{field}_{observer}" for field in PERSISTED_FIELDS}


async def _write_batches(work: Callable[..., Awaitable[None]], items: Iterable) -> None:
    """One session (and transaction) per batch."""
    it = iter(items)
    while batch := list(islice(it, BATCH_SIZE)):
        async with neo4j_driver.session() as session:
            await work(session, batch)


async def _clear_unreached(keys: dict[str, str], scored: set[str]) -> int:
    """Clear the observer's scores on nodes it no longer reaches."""
    key_list = list(keys.values())
    async with neo4j_driver.session() as session:
        holders = await get_pubkeys_scored_by_observer(session, key_list)
    stale = [pk for pk in holders if pk not in scored]
    await _write_batches(
        lambda session, batch: clear_observer_scores(session, batch, key_list), stale
    )
    return len(stale)


def cleanup_due(last: datetime | None, interval_days: int, now: datetime) -> bool:
    """Out-of-reach scores are cleared by time, not run count: cadences range from
    hours to months."""
    if interval_days <= 0:
        return False
    return last is None or now - last >= timedelta(days=interval_days)


async def process_neo4j_write_message(message: dict):
    run_id = message["private_id"]
    is_success = message["result"]["success"]
    logger.info(f"neo4j write run={run_id}")
    logger.info(is_success)
    # if not is_success:
    #     return

    logger.info(f"Writing results to Neo4j... run={run_id}")
    grape_rank_result: GrapeRankResult = message["result"]
    scorecards = grape_rank_result.get("scorecards")
    if not scorecards:
        return

    observer = next(iter(scorecards.values()))["observer"]
    keys = _keys(observer)
    write_set = grape_rank_result.get("neo4jWritePubkeys")
    clear_set = grape_rank_result.get("neo4jClearPubkeys") or []
    floor = grape_rank_result.get("neo4jPersistFloor")

    # Map built here, not interpolated into the Cypher: see app/repos/CLAUDE.md.
    def row(card: ScoreCard) -> dict:
        return {
            "pubkey": card["observee"],
            "props": {
                keys[field]: cast(card.get(field, 0))
                for field, cast in PERSISTED_FIELDS.items()
            },
        }

    async with db_session() as db:
        request_row = await select_brainstorm_request_by_id_on_db(db, run_id)
        forced = bool(request_row.force_full_neo4j)
        # The clear costs a label scan, so it runs on forced runs and then by time.
        clear_unreached = forced or cleanup_due(
            await get_last_neo4j_cleanup_at_on_db(db, observer),
            settings.neo4j_cleanup_interval_days,
            utc_now(),
        )
        # An older worker sends no write set.
        full = settings.neo4j_full_write or forced or write_set is None
        await update_brainstorm_request_internal_publication_status_by_id_on_db(
            db,
            brainstorm_request_id=message["private_id"],
            status=BrainstormRequestStatus.ONGOING,
        )

        await db.commit()

    start_time = time.time()

    cards: list[ScoreCard] = list(scorecards.values())
    if not full:
        changed = set(write_set or ())
        cards = [c for c in cards if c["observee"] in changed]
    elif floor is not None:
        cards = [c for c in cards if c["influence"] > floor]

    # Rows built a batch at a time, so the event loop is never held for all of them.
    await _write_batches(set_observer_scores, (row(card) for card in cards))
    written = len(cards)

    key_list = list(keys.values())
    await _write_batches(
        lambda session, batch: clear_observer_scores(session, batch, key_list),
        clear_set,
    )

    cleared = 0
    if clear_unreached:
        scored = {c["observee"] for c in scorecards.values()}
        cleared = await _clear_unreached(keys, scored)

    async with db_session() as db:
        await update_brainstorm_request_internal_publication_status_by_id_on_db(
            db,
            brainstorm_request_id=message["private_id"],
            status=BrainstormRequestStatus.SUCCESS,
        )
        if clear_unreached:
            await mark_neo4j_cleanup_on_db(db, observer)

        await db.commit()

    final_time = time.time() - start_time
    logger.info(
        f"Took {final_time:.2f} seconds to process Neo4j writes "
        f"(mode={'full' if full else 'changed'} written={written} of {len(scorecards)} "
        f"below_floor_cleared={len(clear_set)} cleared={cleared}) run={run_id} "
        f"observer={observer}"
    )
