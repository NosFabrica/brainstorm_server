"""Turning a Flash activation into a paid scheduling policy.

Entitlement is the scheduling assignment; the subscription record only explains
it. So these assert *which policy a pubkey ends up on*, and that nothing moves a
user we can't confidently account for.

Repos and the Flash lookup are mocked — this is orchestration, and the point is
which calls happen (and which don't), not persistence.
"""

import asyncio
from datetime import datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.core.flash import FlashSubscription, FlashUnavailable
from app.services.billing_service import (
    EntitlementOutcome,
    apply_entitlement,
    grants_entitlement,
)

PUBKEY = "a" * 64
PAID_SCHEDULING_ID = 7
SUBSCRIPTION_ID = "7d3b"


def _subscription(status: str = "active", **overrides) -> FlashSubscription:
    return FlashSubscription(
        id=SUBSCRIPTION_ID,
        status=status,
        ref=PUBKEY,
        subscriber_id="a91c",
        service_id="9c1e",
        plan_id="4f2a",
        current_period_start=datetime(2026, 8, 20, 14, 3, 11),
        current_period_end=datetime(2026, 9, 20, 14, 3, 11),
        next_billing_date=datetime(2026, 9, 20, 14, 3, 11),
        trial_end_date=None,
        cancel_effective_date=None,
        **overrides,
    )


def _plan(scheduling_id: int = PAID_SCHEDULING_ID) -> SimpleNamespace:
    return SimpleNamespace(
        id=1,
        flash_service_id="9c1e",
        flash_plan_id="4f2a",
        subscription_tier="priority",
        scheduling_id=scheduling_id,
        amount_minor=200,
        currency="USD",
        is_active=True,
    )


@pytest.fixture
def billing(monkeypatch):
    """Every seam apply_entitlement touches, defaulted to the happy path."""
    seams = SimpleNamespace(
        db=AsyncMock(),
        user_exists=AsyncMock(return_value=True),
        fetch=AsyncMock(return_value=_subscription()),
        plan=AsyncMock(return_value=_plan()),
        blocked=AsyncMock(return_value=False),
        policy=AsyncMock(return_value=SimpleNamespace(id=PAID_SCHEDULING_ID, enabled=True)),
        existing=AsyncMock(return_value=None),
        set_scheduling=AsyncMock(),
        upsert=AsyncMock(),
    )
    for name, mock in (
        ("brainstorm_nsec_exists_by_pubkey_on_db", seams.user_exists),
        ("fetch_subscription", seams.fetch),
        ("get_billing_plan_on_db", seams.plan),
        ("is_billing_blocked_on_db", seams.blocked),
        ("get_scheduling_on_db", seams.policy),
        ("get_user_subscription_for_update_on_db", seams.existing),
        ("set_scheduling_for_pubkey_on_db", seams.set_scheduling),
        ("upsert_user_subscription_on_db", seams.upsert),
    ):
        monkeypatch.setattr(f"app.services.billing_service.{name}", mock)
    return seams


def _apply(billing, ref: str | None = PUBKEY) -> EntitlementOutcome:
    """Sync wrapper — the suite has no pytest-asyncio; see tests/test_manual_quota.py."""
    return asyncio.run(
        apply_entitlement(
            billing.db, external_ref=ref, subscription_id=SUBSCRIPTION_ID
        )
    )


# ---------------------------------------------------------------------------
# Which statuses entitle (slice 03 completes the table)
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("status", ["active", "trial"])
def test_paid_and_trialing_subscriptions_entitle(status):
    assert grants_entitlement(status) is True


@pytest.mark.parametrize("status", ["pending", "past_due", "paused", "canceled", "expired"])
def test_everything_else_does_not_entitle(status):
    assert grants_entitlement(status) is False


def test_a_status_we_have_never_seen_does_not_entitle():
    """Flash documents the set as open. An unknown value must not grant."""
    assert grants_entitlement("subscription.quantum_superposition") is False


# ---------------------------------------------------------------------------
# The happy path
# ---------------------------------------------------------------------------
def test_activation_moves_the_user_onto_the_paid_policy(billing):
    outcome = _apply(billing)

    assert outcome.applied is True
    billing.set_scheduling.assert_awaited_once()
    args, kwargs = billing.set_scheduling.await_args
    assert args[1] == PUBKEY
    assert args[2] == PAID_SCHEDULING_ID
    assert kwargs["source"] == "billing"


def test_the_subscription_record_captures_plan_policy_period_and_status(billing):
    _apply(billing)

    recorded = billing.upsert.await_args.kwargs
    assert recorded["pubkey"] == PUBKEY
    assert recorded["billing_plan_id"] == 1
    assert recorded["granted_scheduling_id"] == PAID_SCHEDULING_ID
    assert recorded["current_period_end"] == datetime(2026, 9, 20, 14, 3, 11)
    assert recorded["flash_status"] == "active"


def test_flash_status_is_recorded_verbatim_not_translated(billing):
    billing.fetch.return_value = _subscription(status="trial")

    _apply(billing)

    assert billing.upsert.await_args.kwargs["flash_status"] == "trial"


def test_what_we_granted_is_recorded_not_what_the_plan_now_says(billing):
    """Retuning a plan later must not strand whoever it already granted."""
    billing.plan.return_value = _plan(scheduling_id=99)

    _apply(billing)

    assert billing.upsert.await_args.kwargs["granted_scheduling_id"] == 99
    assert billing.set_scheduling.await_args.args[2] == 99


def test_entitlement_reads_flash_rather_than_trusting_the_event(billing):
    _apply(billing)

    billing.fetch.assert_awaited_once()
    assert billing.fetch.await_args.kwargs["subscription_id"] == SUBSCRIPTION_ID


# ---------------------------------------------------------------------------
# Users and plans we can't account for
# ---------------------------------------------------------------------------
def test_an_unknown_reference_moves_nobody_and_is_flagged(billing):
    billing.user_exists.return_value = False

    outcome = _apply(billing)

    assert outcome.applied is False
    assert outcome.reason == "unknown_user"
    billing.set_scheduling.assert_not_awaited()


def test_a_missing_reference_moves_nobody(billing):
    outcome = _apply(billing, ref=None)

    assert outcome.applied is False
    assert outcome.reason == "no_reference"
    billing.set_scheduling.assert_not_awaited()
    billing.fetch.assert_not_awaited()


def test_an_unrecognised_plan_moves_nobody(billing):
    billing.plan.return_value = None

    outcome = _apply(billing)

    assert outcome.applied is False
    assert outcome.reason == "unknown_plan"
    billing.set_scheduling.assert_not_awaited()


def test_an_unreachable_flash_moves_nobody(billing):
    billing.fetch.side_effect = FlashUnavailable("vault down")

    outcome = _apply(billing)

    assert outcome.applied is False
    assert outcome.reason == "sync_failed"
    billing.set_scheduling.assert_not_awaited()
    billing.db.commit.assert_not_awaited()


def test_a_subscription_that_does_not_entitle_moves_nobody(billing):
    billing.fetch.return_value = _subscription(status="pending")

    outcome = _apply(billing)

    assert outcome.applied is False
    billing.set_scheduling.assert_not_awaited()


def test_a_lapsed_status_keeps_the_record_of_what_was_granted(billing):
    """Blanking it would leave slice 03 with nothing to take back, while the
    user sits on a tier no record accounts for."""
    billing.existing.return_value = SimpleNamespace(granted_scheduling_id=PAID_SCHEDULING_ID)
    billing.fetch.return_value = _subscription(status="past_due")

    _apply(billing)

    assert billing.upsert.await_args.kwargs["granted_scheduling_id"] == PAID_SCHEDULING_ID
    billing.set_scheduling.assert_not_awaited()


def test_granting_onto_a_disabled_policy_is_escalated_not_silently_done(
    billing, monkeypatch
):
    """They are paying for a tier that will never run. Re-enabling fixes everyone
    at once, so grant — but say so loudly rather than waiting for a report.
    (Asserted on the logger, not caplog: the repo's loggr doesn't propagate.)"""
    billing.policy.return_value = SimpleNamespace(id=PAID_SCHEDULING_ID, enabled=False)
    errors = MagicMock()
    monkeypatch.setattr("app.services.billing_service.logger.error", errors)

    outcome = _apply(billing)

    assert outcome.applied is True
    errors.assert_called_once()


def test_an_enabled_policy_raises_no_alarm(billing, monkeypatch):
    errors = MagicMock()
    monkeypatch.setattr("app.services.billing_service.logger.error", errors)

    _apply(billing)

    errors.assert_not_called()


def test_the_subscribers_row_is_locked_before_anything_is_decided(billing):
    billing.existing.assert_not_awaited()

    _apply(billing)

    billing.existing.assert_awaited_once()


# ---------------------------------------------------------------------------
# Admin decisions vs paying for something
# ---------------------------------------------------------------------------
def test_a_paying_user_is_granted_even_where_an_admin_last_set_the_policy(billing):
    """An admin assignment stops billing taking a tier AWAY (slice 03); it must
    never stop someone receiving what they are being charged for."""
    outcome = _apply(billing)

    assert outcome.applied is True
    billing.set_scheduling.assert_awaited_once()


def test_granting_hands_the_policy_back_to_billing(billing):
    _apply(billing)

    assert billing.set_scheduling.await_args.kwargs["source"] == "billing"


# ---------------------------------------------------------------------------
# Blocking
# ---------------------------------------------------------------------------
def test_a_blocked_user_is_never_granted_even_while_paying(billing):
    billing.blocked.return_value = True

    outcome = _apply(billing)

    assert outcome.applied is False
    assert outcome.reason == "blocked"
    billing.set_scheduling.assert_not_awaited()


def test_a_blocked_user_still_has_their_subscription_recorded(billing):
    """They are still being charged, so support must be able to see it — and
    they must still be able to cancel and leave."""
    billing.blocked.return_value = True

    _apply(billing)

    billing.upsert.assert_awaited_once()
    assert billing.upsert.await_args.kwargs["flash_status"] == "active"


# ---------------------------------------------------------------------------
# Atomicity
# ---------------------------------------------------------------------------
def test_the_record_and_the_assignment_commit_together(billing):
    _apply(billing)

    assert billing.upsert.await_count == 1
    assert billing.set_scheduling.await_count == 1
    billing.db.commit.assert_awaited_once()


def test_a_failed_assignment_commits_nothing(billing):
    """They must never disagree — a tier without a record, or the reverse."""
    billing.set_scheduling.side_effect = RuntimeError("db died mid-write")

    with pytest.raises(RuntimeError):
        _apply(billing)

    billing.db.commit.assert_not_awaited()


def test_a_failed_record_commits_nothing(billing):
    billing.upsert.side_effect = RuntimeError("db died mid-write")

    with pytest.raises(RuntimeError):
        _apply(billing)

    billing.db.commit.assert_not_awaited()
