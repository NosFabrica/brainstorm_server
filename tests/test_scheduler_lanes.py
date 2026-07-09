"""Behavioral tests for scheduler-lane routing.

`resolve_scheduler_lane` is the single seam that decides which Redis calc queue
a recalculation request goes to, given its trigger source and (for scheduled
work) the user's scheduling policy. Lanes are off by default and collapse to
today's single `message_queue`, so an un-configured deploy behaves as before.
"""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

from app.core.config import settings
from app.repos.brainstorm_request_repo import create_brainstorm_request_on_db
from app.services.scheduler_lanes import enqueue_calc_request, resolve_scheduler_lane

PK = "a" * 64


def _fake_redis(monkeypatch):
    redis = SimpleNamespace(rpush=AsyncMock())
    monkeypatch.setattr("app.services.scheduler_lanes.redis_client", redis)
    return redis


def test_manual_routes_to_manual_lane_default_single_queue():
    # Default config: every lane collapses to today's single queue.
    assert resolve_scheduler_lane("manual") == "message_queue"


def test_admin_routes_to_admin_lane_when_enabled(monkeypatch):
    monkeypatch.setattr(settings, "scheduler_enabled", True)
    assert resolve_scheduler_lane("admin") == "sched:admin"


def test_periodic_routes_to_house_lane_when_enabled(monkeypatch):
    monkeypatch.setattr(settings, "scheduler_enabled", True)
    assert resolve_scheduler_lane("periodic") == "sched:house"


def test_manual_keeps_message_queue_lane_when_enabled(monkeypatch):
    # Manual keeps the existing queue name even with lanes on (no rename).
    monkeypatch.setattr(settings, "scheduler_enabled", True)
    assert resolve_scheduler_lane("manual") == "message_queue"


def test_scheduled_routes_to_lane_for_policy_priority(monkeypatch):
    # "priority is the lane": the policy's priority picks the scheduled lane.
    monkeypatch.setattr(settings, "scheduler_enabled", True)
    policy = SimpleNamespace(priority=2)
    assert resolve_scheduler_lane("scheduled", policy) == "sched:2"


def test_scheduled_without_policy_uses_priority_zero_lane(monkeypatch):
    monkeypatch.setattr(settings, "scheduler_enabled", True)
    assert resolve_scheduler_lane("scheduled", None) == "sched:0"


def test_disabled_collapses_every_source_to_single_queue():
    # Default (off): admin/periodic/scheduled all drain the one existing queue.
    for source in ("manual", "admin", "periodic", "scheduled"):
        assert resolve_scheduler_lane(source, SimpleNamespace(priority=5)) == "message_queue"


def test_create_request_records_trigger_source():
    db = AsyncMock()
    added = {}
    db.add = lambda obj: added.__setitem__("obj", obj)  # session.add is sync

    asyncio.run(
        create_brainstorm_request_on_db(
            db,
            algorithm="graperank",
            parameters=PK,
            pubkey=PK,
            trigger_source="admin",
        )
    )

    assert added["obj"].trigger_source == "admin"


def test_enqueue_rpushes_to_resolved_lane(monkeypatch):
    monkeypatch.setattr(settings, "scheduler_enabled", True)
    redis = _fake_redis(monkeypatch)
    instance = SimpleNamespace(model_dump_json=lambda: "{}")

    asyncio.run(enqueue_calc_request(AsyncMock(), instance, PK, "admin"))

    redis.rpush.assert_awaited_once_with("sched:admin", "{}")


def test_enqueue_scheduled_uses_policy_priority_lane(monkeypatch):
    monkeypatch.setattr(settings, "scheduler_enabled", True)
    redis = _fake_redis(monkeypatch)
    monkeypatch.setattr(
        "app.services.scheduler_lanes.get_scheduling_for_pubkey_on_db",
        AsyncMock(return_value=SimpleNamespace(priority=2)),
    )
    instance = SimpleNamespace(model_dump_json=lambda: "{}")

    asyncio.run(enqueue_calc_request(AsyncMock(), instance, PK, "scheduled"))

    redis.rpush.assert_awaited_once_with("sched:2", "{}")
