"""Surviving crashes, redeliveries and races.

Flash acknowledges nothing twice: it retries an undelivered webhook a few times
and then never replays it. So once we answer 200, the event is ours to not lose
— which is what the claim/complete markers and the replay pass are for.
"""

import asyncio
from datetime import datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.services.billing_service import EntitlementReason
from app.services.billing_sync_service import (
    replay_unprocessed_events,
)

NOW = datetime(2026, 8, 25, 12, 0, 0)
PUBKEY = "a" * 64


def _event(event_id=1, external_ref=PUBKEY, subscription_id="7d3b", attempts=0):
    return SimpleNamespace(
        id=event_id,
        event="subscription.activated",
        event_timestamp=NOW,
        payload={"data": {"externalRef": external_ref, "subscriptionId": subscription_id}},
        attempts=attempts,
    )


@pytest.fixture
def replay(monkeypatch):
    seams = SimpleNamespace(
        db=AsyncMock(),
        abandoned=AsyncMock(return_value=[_event()]),
        claim=AsyncMock(return_value=True),
        apply=AsyncMock(
            return_value=SimpleNamespace(
                applied=True, reason=EntitlementReason.GRANTED
            )
        ),
        complete=AsyncMock(),
        fail=AsyncMock(),
    )
    for name, mock in (
        ("select_abandoned_webhook_events_on_db", seams.abandoned),
        ("claim_webhook_event_on_db", seams.claim),
        ("apply_entitlement", seams.apply),
        ("mark_webhook_event_processed_on_db", seams.complete),
        ("record_webhook_event_failure_on_db", seams.fail),
    ):
        monkeypatch.setattr(f"app.services.billing_sync_service.{name}", mock)
    return seams


def _run(replay, limit=25, max_attempts=5):
    return asyncio.run(
        replay_unprocessed_events(
            replay.db,
            limit=limit,
            stale_after=timedelta(minutes=5),
            max_attempts=max_attempts,
        )
    )


# ---------------------------------------------------------------------------
# Nothing acknowledged is lost
# ---------------------------------------------------------------------------
def test_an_abandoned_event_is_picked_up_and_applied(replay):
    """Acknowledged, then the process died. Flash will not send it again."""
    assert _run(replay) == 1

    replay.apply.assert_awaited_once()
    assert replay.apply.await_args.kwargs["external_ref"] == PUBKEY
    assert replay.apply.await_args.kwargs["subscription_id"] == "7d3b"


def test_an_applied_event_is_marked_so_it_is_not_applied_again(replay):
    _run(replay)

    replay.complete.assert_awaited_once()
    assert replay.complete.await_args.args[1] == 1


def test_an_event_another_worker_already_claimed_is_left_alone(replay):
    """The claim is what makes it exactly-once across replicas — losing the race
    means someone else has it, not that it needs doing twice."""
    replay.claim.return_value = False

    assert _run(replay) == 0
    replay.apply.assert_not_awaited()


def test_work_in_progress_is_not_swept_up_as_abandoned(replay):
    _run(replay)

    # The staleness window is what separates "a worker has this" from
    # "a worker died holding this".
    assert replay.abandoned.await_args.kwargs["stale_after"] == timedelta(minutes=5)


def test_a_failure_is_recorded_against_the_event_rather_than_lost(replay):
    replay.apply.side_effect = RuntimeError("neo4j on fire")

    assert _run(replay) == 0
    replay.fail.assert_awaited_once()
    replay.complete.assert_not_awaited()


def test_one_failing_event_does_not_stop_the_others(replay):
    replay.abandoned.return_value = [_event(1), _event(2)]
    replay.apply.side_effect = [
        RuntimeError("blip"),
        SimpleNamespace(applied=True, reason=EntitlementReason.GRANTED),
    ]

    assert _run(replay) == 1


def test_replay_is_bounded(replay):
    _run(replay, limit=7)

    assert replay.abandoned.await_args.kwargs["limit"] == 7


def test_an_event_that_keeps_failing_stops_being_retried(replay):
    """Bounded, so a permanently broken event does not occupy every cycle
    forever — it is surfaced instead."""
    _run(replay, max_attempts=3)

    assert replay.abandoned.await_args.kwargs["max_attempts"] == 3


def test_an_outcome_that_settled_nothing_leaves_the_event_for_another_go(replay):
    """Marking it done would quietly discard the work."""
    replay.apply.return_value = SimpleNamespace(
        applied=False, reason=EntitlementReason.BUSY
    )

    assert _run(replay) == 0
    replay.complete.assert_not_awaited()
    replay.fail.assert_awaited_once()


def test_an_event_with_no_payload_is_surfaced_rather_than_marked_done(replay):
    """Nothing writes a null payload, so this is a hand-edited row — there is
    nothing left to apply, and marking it processed would hide that."""
    empty = _event()
    empty.payload = None
    replay.abandoned.return_value = [empty]

    assert _run(replay) == 0
    replay.apply.assert_not_awaited()
    replay.complete.assert_not_awaited()
    replay.fail.assert_awaited_once()
