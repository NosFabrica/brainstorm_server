import asyncio
from datetime import datetime, timedelta

from app.core.config import settings
from app.core.database import db_session
from app.core.loggr import loggr
from app.repos.brainstorm_nsec import (
    get_or_create_brainstorm_observer_nsec_by_pubkey_on_db,
)
from app.repos.brainstorm_request_repo import (
    select_latest_non_waiting_brainstorm_request_on_db,
)
from app.services.brainstorm_request_service import create_brainstorm_request

logger = loggr.get_logger(__name__)

PERIOD_HOURS = 2


def _previous_aligned_mark(now: datetime) -> datetime:
    aligned_hour = (now.hour // PERIOD_HOURS) * PERIOD_HOURS
    return now.replace(hour=aligned_hour, minute=0, second=0, microsecond=0)


def _next_aligned_mark(now: datetime) -> datetime:
    return _previous_aligned_mark(now) + timedelta(hours=PERIOD_HOURS)


async def _trigger_graperank(pubkey: str, reason: str) -> None:
    async with db_session() as db:
        triggered = await create_brainstorm_request(
            db=db,
            algorithm="graperank",
            parameters=pubkey,
            pubkey=pubkey,
            nsec_exists=True,
        )
    logger.info(
        f"Periodic graperank cronjob: triggered graperank "
        f"(reason={reason}, pubkey={pubkey}, request_id={triggered.private_id})"
    )


async def periodic_graperank_trigger_cronjob() -> None:
    pubkey = (settings.periodic_graperank_pubkey or "").strip()
    if not pubkey:
        logger.info(
            "Periodic graperank cronjob: PERIODIC_GRAPERANK_PUBKEY is empty, "
            "cronjob will not run"
        )
        return

    async with db_session() as db:
        _, created = await get_or_create_brainstorm_observer_nsec_by_pubkey_on_db(
            db, pubkey=pubkey
        )

    if created:
        logger.info(
            f"Periodic graperank cronjob: created BrainstormNsec row for "
            f"pubkey={pubkey}"
        )

    logger.info(
        f"Periodic graperank cronjob started (pubkey={pubkey}, "
        f"every {PERIOD_HOURS}h, aligned to 00:00)"
    )

    # Startup catch-up: if the latest non-waiting graperank was triggered before
    # the most recent aligned mark, trigger now so we don't skip a window.
    try:
        now = datetime.now()
        previous_mark = _previous_aligned_mark(now)
        async with db_session() as db:
            latest = await select_latest_non_waiting_brainstorm_request_on_db(
                db, pubkey=pubkey
            )
        latest_str = latest.created_at.isoformat() if latest else "none"
        if latest is None or latest.created_at < previous_mark:
            logger.info(
                f"Periodic graperank cronjob: startup catch-up needed "
                f"(previous_mark={previous_mark.isoformat()}, "
                f"latest_non_waiting={latest_str})"
            )
            await _trigger_graperank(pubkey, reason="startup_catchup")
        else:
            logger.info(
                f"Periodic graperank cronjob: startup catch-up not needed "
                f"(latest_non_waiting={latest_str} >= "
                f"previous_mark={previous_mark.isoformat()})"
            )
    except Exception as e:
        logger.error(f"Periodic graperank cronjob startup catch-up errored! {e}")

    while True:
        try:
            now = datetime.now()
            next_mark = _next_aligned_mark(now)
            sleep_seconds = max(0.0, (next_mark - now).total_seconds())
            logger.info(
                f"Periodic graperank cronjob: sleeping {sleep_seconds:.0f}s "
                f"until next mark {next_mark.isoformat()}"
            )
            await asyncio.sleep(sleep_seconds)
            logger.info(
                f"Periodic graperank cronjob: woke at "
                f"{datetime.now().isoformat()} (scheduled {next_mark.isoformat()})"
            )
            await _trigger_graperank(pubkey, reason="scheduled_mark")
        except Exception as e:
            logger.error(f"Periodic graperank cronjob errored! {e}")
            # Avoid a tight retry loop on persistent errors.
            await asyncio.sleep(60)
