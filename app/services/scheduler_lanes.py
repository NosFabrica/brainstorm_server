"""Routing of calc requests to Redis priority lanes. Off (scheduler_enabled
False) every source uses the single message_queue; on, requests split by source
and, for scheduled work, the policy's priority ("priority is the lane").
"""

import json

from app.core.config import settings
from app.core.redis_db import redis_client
from app.db_models import TriggerSource
from app.repos.brainstorm_nsec import get_scheduling_for_pubkey_on_db
from app.repos.compromised_pubkey_repo import select_compromised_pubkeys_on_db

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


async def enqueue_calc_request(
    db,
    instance,
    pubkey: str,
    trigger_source: str,
    designated_pubkeys: list[str] | None = None,
) -> None:
    """Resolve the lane for this request and rpush it.

    `designated_pubkeys` (the Observer's kind-10040 keys) ride on the queue
    message only — the worker scores them at a fixed 95 — not on the API schema.
    So do `compromised_pubkeys`, read here so a run queued after an account is
    marked holds it at Influence 0: nothing it follows, mutes, reports or
    designates counts (`services/compromised_service.py`).
    """
    scheduling = None
    if trigger_source == TriggerSource.SCHEDULED.value:
        scheduling = await get_scheduling_for_pubkey_on_db(db, pubkey)
    lane = resolve_scheduler_lane(trigger_source, scheduling)
    compromised = sorted(await select_compromised_pubkeys_on_db(db))
    if designated_pubkeys or compromised:
        payload = instance.model_dump(mode="json")
        if designated_pubkeys:
            payload["designated_pubkeys"] = designated_pubkeys
        if compromised:
            payload["compromised_pubkeys"] = compromised
        message = json.dumps(payload)
    else:
        message = instance.model_dump_json()
    await redis_client.rpush(lane, message)  # type: ignore[misc]
