"""Auto-scheduler loop. Disabled unless settings.scheduler_enabled.

Each cycle, under a Redis leader-lock so exactly one instance schedules, admits
the most-overdue eligible users (highest tier first) up to the publish-stage
in-flight target, skipping any with no follows or a run in flight.
"""

import asyncio
import socket
import uuid
from datetime import datetime

from app.core.config import settings
from app.core.database import db_session
from app.core.loggr import loggr
from app.core.redis_db import redis_client
from app.db_models import TriggerSource
from app.neo4j_db.driver import driver as neo4j_driver
from app.repos.brainstorm_request_repo import (
    any_interactive_in_pipeline_on_db,
    count_scheduled_publishing_inflight_on_db,
)
from app.repos.scheduler_repo import load_scheduler_candidates_on_db
from app.repos.scheduling_repo import get_default_scheduling_on_db
from app.repos.user_repo import count_user_follows
from app.services.brainstorm_request_service import create_brainstorm_request
from app.services.scheduler import (
    admission_budget,
    choose_admission_lane,
    rank_overdue_candidates,
)
from app.services.scheduler_lock import acquire_or_renew_leader

logger = loggr.get_logger(__name__)

SCHEDULER_INTERVAL_SECONDS = 60
LEADER_LOCK_TTL_MS = 120_000
_INSTANCE_ID = f"{socket.gethostname()}:{uuid.uuid4()}"

# Cumulative admitted count per priority lane, for weighted fairness. Leader-only
# in-memory state; resets on restart/leader change (best-effort by design).
_admitted_counts: dict[int, int] = {}


async def _run_cycle(db) -> None:
    inflight = await count_scheduled_publishing_inflight_on_db(db)
    interactive = await any_interactive_in_pipeline_on_db(db)
    budget = admission_budget(settings.scheduler_inflight_target, inflight, interactive)
    if budget <= 0:
        logger.info(
            f"Scheduler: admission paused (inflight={inflight}, interactive={interactive})."
        )
        return

    default = await get_default_scheduling_on_db(db)
    if default is None:
        logger.warning("No default scheduling policy; skipping cycle.")
        return

    candidates = await load_scheduler_candidates_on_db(
        db,
        platform_pubkey=settings.periodic_graperank_pubkey,
        default_priority=default.priority,
        default_interval_seconds=default.schedule_interval_seconds,
        default_enabled=default.enabled,
    )
    ranked = rank_overdue_candidates(candidates, datetime.now())

    # Bucket into priority lanes (most-overdue first within each, from `ranked`).
    lanes: dict[int, list] = {}
    for candidate in ranked:
        lanes.setdefault(candidate.priority, []).append(candidate)

    enqueued = 0
    while enqueued < budget and any(lanes.values()):
        active = [p for p, queue in lanes.items() if queue]
        if settings.scheduler_fairness_enabled:
            lane = choose_admission_lane(active, _admitted_counts)
        else:
            lane = max(active)  # strict highest-priority-first
        candidate = lanes[lane].pop(0)
        async with neo4j_driver.session() as session:
            if await count_user_follows(session, candidate.pubkey) <= 0:
                continue  # no grapevine — picked up once they follow someone
        await create_brainstorm_request(
            db=db,
            algorithm="graperank",
            parameters=candidate.pubkey,
            pubkey=candidate.pubkey,
            nsec_exists=True,
            trigger_source=TriggerSource.SCHEDULED.value,
        )
        _admitted_counts[lane] = _admitted_counts.get(lane, 0) + 1
        enqueued += 1

    if ranked:
        logger.info(f"Scheduler: {len(ranked)} overdue, enqueued {enqueued}/{budget}.")


async def scheduler_cronjob() -> None:
    if not settings.scheduler_enabled:
        logger.info("Scheduler disabled; not starting.")
        return
    logger.info(f"Scheduler starting (instance {_INSTANCE_ID}).")
    while True:
        try:
            async with db_session() as db:
                if await acquire_or_renew_leader(
                    redis_client, _INSTANCE_ID, LEADER_LOCK_TTL_MS
                ):
                    await _run_cycle(db)
        except Exception as exc:
            logger.error(f"Scheduler cycle error: {exc}")
        await asyncio.sleep(SCHEDULER_INTERVAL_SECONDS)
