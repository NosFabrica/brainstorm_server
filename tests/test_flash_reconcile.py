"""Re-reading Flash for subscribers no event has settled.

Flash retries a failed delivery a few times and then never replays it, so this
is the only path that recovers a lost webhook. It is also the only thing that
can resolve the two states the lifecycle sweep deliberately refuses to judge:
a `past_due` row, and one still recorded `active` past its period end. Locally
those are indistinguishable from "renewal succeeded and we missed the event".
"""

import asyncio
from datetime import datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.core.flash import FlashCredentialError, FlashUnavailable
from app.services.billing_service import EntitlementReason
from app.services.billing_sync_service import reconcile_subscriptions

NOW = datetime(2026, 8, 25, 12, 0, 0)
STALE = NOW - timedelta(days=30)
PUBKEY = "a" * 64


def _row(pubkey=PUBKEY, status="past_due", period_end=STALE, synced=STALE):
    return SimpleNamespace(
        pubkey=pubkey,
        flash_status=status,
        current_period_end=period_end,
        last_synced_at=synced,
    )


@pytest.fixture
def reconcile(monkeypatch):
    seams = SimpleNamespace(
        db=AsyncMock(),
        candidates=AsyncMock(return_value=[_row()]),
        apply=AsyncMock(
            return_value=SimpleNamespace(
                applied=True, reason=EntitlementReason.GRANTED
            )
        ),
        record_error=AsyncMock(),
    )
    for name, mock in (
        ("select_reconcile_candidates_on_db", seams.candidates),
        ("apply_entitlement", seams.apply),
        ("record_sync_failure_on_db", seams.record_error),
    ):
        monkeypatch.setattr(f"app.services.billing_sync_service.{name}", mock)
    return seams


def _run(reconcile, limit=25):
    return asyncio.run(
        reconcile_subscriptions(
            reconcile.db,
            limit=limit,
            stale_after=timedelta(hours=6),
            abandon_pending_after=timedelta(hours=24),
            now=NOW,
        )
    )


def test_a_stuck_subscriber_is_re_read_by_their_own_reference(reconcile):
    """No event told us to; that is the point."""
    _run(reconcile)

    reconcile.apply.assert_awaited_once()
    assert reconcile.apply.await_args.kwargs["external_ref"] == PUBKEY
    assert reconcile.apply.await_args.kwargs["subscription_id"] is None


def test_flash_reporting_them_active_restores_the_paid_policy(reconcile):
    """The lost-webhook recovery: entitlement without an event ever arriving."""
    result = _run(reconcile)

    assert result.reconciled == 1
    assert result.failed == 0


def test_an_outage_downgrades_nobody_and_is_recorded_against_them(reconcile):
    reconcile.apply.side_effect = FlashUnavailable("vault down")

    result = _run(reconcile)

    assert result.reconciled == 0
    assert result.failed == 1
    reconcile.record_error.assert_awaited_once()
    assert PUBKEY in reconcile.record_error.await_args.args


def test_an_outage_does_not_stop_the_rest_of_the_batch(reconcile):
    reconcile.candidates.return_value = [_row("a" * 64), _row("b" * 64)]
    reconcile.apply.side_effect = [FlashUnavailable("blip"), SimpleNamespace(
        applied=True, reason=EntitlementReason.GRANTED
    )]

    result = _run(reconcile)

    assert result.reconciled == 1
    assert result.failed == 1


def test_a_credential_failure_stops_the_batch_rather_than_looping(reconcile):
    """It will fail identically for every remaining row. Continuing would bury
    the one thing a human needs to see under a hundred copies of it."""
    reconcile.candidates.return_value = [_row("a" * 64), _row("b" * 64)]
    reconcile.apply.side_effect = FlashCredentialError("refused")

    result = _run(reconcile)

    assert reconcile.apply.await_count == 1
    assert result.failed == 1


def test_the_batch_is_bounded(reconcile):
    _run(reconcile, limit=5)

    assert reconcile.candidates.await_args.kwargs["limit"] == 5


def test_nothing_to_reconcile_asks_flash_nothing(reconcile):
    reconcile.candidates.return_value = []

    result = _run(reconcile)

    assert result == SimpleNamespace(reconciled=0, failed=0) or (
        result.reconciled == 0 and result.failed == 0
    )
    reconcile.apply.assert_not_awaited()


def test_recording_a_failure_advances_the_read_clock(monkeypatch):
    """Candidates are ordered oldest-read-first and the batch is bounded, so a
    handful of permanent failures would otherwise hold the front of the queue
    forever and nobody else would ever be reconciled."""
    from app.repos.user_subscription_repo import record_sync_failure_on_db

    captured = {}

    async def _capture(db, statement, name):
        captured["values"] = dict(statement.compile().params)

    monkeypatch.setattr("app.repos.user_subscription_repo.execute_db_statement", _capture)

    asyncio.run(record_sync_failure_on_db(AsyncMock(), PUBKEY, "vault down"))

    assert captured["values"]["last_sync_error"] == "vault down"
    assert captured["values"]["last_synced_at"] is not None


def test_flash_having_no_such_subscription_is_not_counted_as_reconciled(reconcile):
    """It never reaches the upsert, so nothing recorded that we asked — and the
    candidate ordering would park them at the head of the batch forever."""
    reconcile.apply.return_value = SimpleNamespace(
        applied=False, reason=EntitlementReason.UNKNOWN_SUBSCRIPTION
    )

    result = _run(reconcile)

    assert result.reconciled == 0
    assert result.failed == 1
    reconcile.record_error.assert_awaited_once()


def test_the_sweep_asks_for_abandoned_checkouts_to_be_left_out(reconcile):
    """The window and the error string are the query's, not the sweep's — but a
    caller that forgot to pass them would silently sweep them forever.

    What the query then does with them is `tests/integration/
    test_billing_reconcile_candidates_integration.py`.
    """
    _run(reconcile)

    kwargs = reconcile.candidates.await_args.kwargs
    assert kwargs["abandon_pending_after"] == timedelta(hours=24)
    assert kwargs["abandoned_error"] == EntitlementReason.UNKNOWN_SUBSCRIPTION.value


def test_an_unmapped_plan_also_advances_the_read_clock(reconcile):
    reconcile.apply.return_value = SimpleNamespace(
        applied=False, reason=EntitlementReason.UNKNOWN_PLAN
    )

    _run(reconcile)

    reconcile.record_error.assert_awaited_once()


def test_a_held_subscription_counts_as_reconciled(reconcile):
    """HOLD still refreshed their status and period — that IS the resolution."""
    reconcile.apply.return_value = SimpleNamespace(
        applied=False, reason=EntitlementReason.HELD
    )

    result = _run(reconcile)

    assert result.reconciled == 1
    reconcile.record_error.assert_not_awaited()
