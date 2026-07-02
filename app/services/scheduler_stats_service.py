"""Assemble the scheduler's self-measured stats for the admin surface."""

import statistics
from datetime import datetime, timedelta

from sqlalchemy.ext.asyncio import AsyncSession as AsyncDBSession

from app.core.redis_db import redis_client
from app.repos.scheduler_metrics_repo import (
    count_published_successes_since_on_db,
    publish_durations_since_on_db,
    scheduling_priorities_on_db,
    tier_demand_slip_rows_on_db,
)
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

    rows = await tier_demand_slip_rows_on_db(db)  # (name, prio, cadence, count, oldest)
    demand = demand_per_day([(row[3], row[2]) for row in rows])
    tier_slip_seconds = {row[0]: tier_slip(row[4], row[2], now) for row in rows}

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
