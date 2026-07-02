"""Assemble the scheduler's self-measured stats for the admin surface."""

import statistics
from datetime import datetime, timedelta

from sqlalchemy.ext.asyncio import AsyncSession as AsyncDBSession

from app.core.redis_db import redis_client
from app.neo4j_db.driver import driver as neo4j_driver
from app.repos.scheduler_metrics_repo import (
    count_published_successes_since_on_db,
    publish_durations_since_on_db,
    scheduling_priorities_on_db,
    tier_users_on_db,
)
from app.repos.user_repo import pubkeys_following_someone
from app.schemas.schemas import SchedulerStats
from app.services.scheduler_lanes import (
    DEFAULT_LANE,
    LANE_ADMIN,
    LANE_HOUSE,
    LANE_SCHEDULED_TEMPLATE,
)
from app.services.scheduler_metrics import (
    demand_per_day,
    throughput_per_day,
    tier_slip,
)


_METRICS_WINDOW_SECONDS = 86400  # 24h


async def get_scheduler_stats(db: AsyncDBSession) -> SchedulerStats:
    now = datetime.now()
    since = now - timedelta(seconds=_METRICS_WINDOW_SECONDS)

    successes = await count_published_successes_since_on_db(db, since)
    throughput = throughput_per_day(successes, _METRICS_WINDOW_SECONDS)

    # Per-user rows: (tier_name, cadence, pubkey, last_published).
    rows = await tier_users_on_db(db)
    cadence_by_tier = {row[0]: row[1] for row in rows}
    counts: dict[str, int] = {}
    for name, _cadence, pubkey, _last in rows:
        counts.setdefault(name, 0)
        if pubkey is not None:
            counts[name] += 1
    demand = demand_per_day([(counts[t], cadence_by_tier[t]) for t in counts])

    # Slip ignores un-schedulable (followerless) users so a stuck no-follows
    # user can't inflate it forever.
    published = [
        (name, pubkey, last) for (name, _c, pubkey, last) in rows
        if pubkey is not None and last is not None
    ]
    async with neo4j_driver.session() as session:
        schedulable = await pubkeys_following_someone(
            session, [pk for (_n, pk, _l) in published]
        )
    oldest_by_tier: dict[str, datetime] = {}
    for name, pubkey, last in published:
        if pubkey not in schedulable:
            continue
        if name not in oldest_by_tier or last < oldest_by_tier[name]:
            oldest_by_tier[name] = last
    tier_slip_seconds = {
        tier: tier_slip(oldest_by_tier.get(tier), cadence_by_tier[tier], now)
        for tier in cadence_by_tier
    }

    durations = await publish_durations_since_on_db(db, since)
    median_publish = statistics.median(durations) if durations else None

    priorities = await scheduling_priorities_on_db(db)
    lane_names = [LANE_ADMIN, LANE_HOUSE, DEFAULT_LANE] + [
        LANE_SCHEDULED_TEMPLATE.format(priority=p) for p in priorities
    ]
    lane_depths = {
        lane: int(await redis_client.llen(lane))
        for lane in dict.fromkeys(lane_names)  # dedupe, keep order
    }

    return SchedulerStats(
        throughput_per_day=throughput,
        demand_per_day=demand,
        median_publish_seconds=median_publish,
        lane_depths=lane_depths,
        tier_slip_seconds=tier_slip_seconds,
    )
