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
from unittest.mock import AsyncMock

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
        tier="priority",
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
        source=AsyncMock(return_value="default"),
        existing=AsyncMock(return_value=None),
        set_scheduling=AsyncMock(),
        upsert=AsyncMock(),
    )
    for name, mock in (
        ("brainstorm_nsec_exists_by_pubkey_on_db", seams.user_exists),
        ("fetch_subscription", seams.fetch),
        ("get_billing_plan_on_db", seams.plan),
        ("get_scheduling_source_on_db", seams.source),
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


def test_the_subscribers_row_is_locked_before_anything_is_decided(billing):
    billing.existing.assert_not_awaited()

    _apply(billing)

    billing.existing.assert_awaited_once()


# ---------------------------------------------------------------------------
# Not overwriting a human's decision
# ---------------------------------------------------------------------------
def test_an_admin_granted_tier_is_not_overwritten_by_billing(billing):
    billing.source.return_value = "admin"

    outcome = _apply(billing)

    assert outcome.reason == "admin_override"
    billing.set_scheduling.assert_not_awaited()


def test_an_admin_granted_tier_still_records_the_subscription(billing):
    """We stop touching their policy, not stop knowing they pay."""
    billing.source.return_value = "admin"

    _apply(billing)

    billing.upsert.assert_awaited_once()


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
