"""Routing of calc requests to Redis priority lanes. Off (scheduler_enabled
False) every source uses the single message_queue; on, requests split by source
and, for scheduled work, the policy's priority ("priority is the lane").
"""

from app.core.config import settings
from app.core.redis_db import redis_client
from app.db_models import TriggerSource
from app.repos.brainstorm_nsec import get_scheduling_for_pubkey_on_db

DEFAULT_LANE = "message_queue"  # manual keeps this name even when lanes are on
LANE_ADMIN = "sched:admin"
LANE_HOUSE = "sched:house"
LANE_SCHEDULED_TEMPLATE = "sched:{priority}"


def resolve_scheduler_lane(trigger_source: str, scheduling=None) -> str:
    if not settings.scheduler_enabled:
        return DEFAULT_LANE
    if trigger_source == TriggerSource.ADMIN.value:
        return LANE_ADMIN
    if trigger_source == TriggerSource.PERIODIC.value:
        return LANE_HOUSE
    if trigger_source == TriggerSource.SCHEDULED.value:
        priority = scheduling.priority if scheduling is not None else 0
        return LANE_SCHEDULED_TEMPLATE.format(priority=priority)
    return DEFAULT_LANE


async def enqueue_calc_request(db, instance, pubkey: str, trigger_source: str) -> None:
    """Resolve the lane for this request and rpush it."""
    scheduling = None
    if trigger_source == TriggerSource.SCHEDULED.value:
        scheduling = await get_scheduling_for_pubkey_on_db(db, pubkey)
    lane = resolve_scheduler_lane(trigger_source, scheduling)
    await redis_client.rpush(lane, instance.model_dump_json())  # type: ignore[misc]
