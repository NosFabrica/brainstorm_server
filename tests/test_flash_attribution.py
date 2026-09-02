"""Resolving a signup that named nobody.

Flash's plain link carries no reference of ours, so the payment arrives as a
webhook event that never becomes a subscriber row — the sweep re-checks it
forever and nobody gets what they paid for. An admin resolves it either way:
onto the person who made it, or as not a customer at all (the live staging case
is the payment provider's own card test).

Two properties carry most of the weight here. The grant runs the *same*
entitlement path a webhook does, so a hand-grant cannot disagree with what the
next event produces; and both outcomes settle the event, which is what stops the
sweep and what lets the payload's email age out under the ordinary prune.
"""

import asyncio
from datetime import datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastapi import HTTPException

from app.core.flash import FlashPricing, FlashSubscription, FlashUnavailable
from app.services.billing_service import (
    SETTLED_REASONS,
    EntitlementOutcome,
    EntitlementReason,
    attribute_unresolved_subscription,
    dismiss_unresolved_subscription,
)
from app.services.billing_service import apply_entitlement as real_apply_entitlement

PUBKEY = "a" * 64
OTHER = "b" * 64
ADMIN = "c" * 64
SUBSCRIPTION_ID = "7d3b"
PAID_SCHEDULING_ID = 7


def _subscription(status: str = "active", ref: str | None = None) -> FlashSubscription:
    """An unattributed signup: Flash knows the subscription, but no ref of ours."""
    return FlashSubscription(
        id=SUBSCRIPTION_ID,
        status=status,
        ref=ref,
        subscriber_id="a91c",
        service_id="9c1e",
        plan_id="4f2a",
        current_period_start=datetime(2026, 8, 20, 14, 3, 11),
        current_period_end=datetime(2026, 9, 20, 14, 3, 11),
        next_billing_date=datetime(2026, 9, 20, 14, 3, 11),
        trial_end_date=None,
        cancel_effective_date=None,
        portal_url="https://flash.example/subscriptions/portal/9c1e",
        pricing=FlashPricing(
            amount_minor=200, currency="USD", billing_interval="monthly"
        ),
    )


def _plan() -> SimpleNamespace:
    return SimpleNamespace(id=1, scheduling_id=PAID_SCHEDULING_ID)


@pytest.fixture
def seams(monkeypatch):
    """Everything the two resolutions touch, defaulted to the happy path.

    `apply_entitlement` is a seam here on purpose: these assert the
    orchestration around it. That it really is the automatic path has its own
    test below, with the seam removed.
    """
    s = SimpleNamespace(
        db=AsyncMock(),
        holder=AsyncMock(return_value=None),
        existing=AsyncMock(return_value=None),
        settle=AsyncMock(return_value=1),
        apply=AsyncMock(
            return_value=EntitlementOutcome(
                applied=True, reason=EntitlementReason.GRANTED
            )
        ),
    )
    for name, mock in (
        ("get_user_subscription_by_flash_id_on_db", s.holder),
        ("get_user_subscription_on_db", s.existing),
        ("settle_unresolved_events_on_db", s.settle),
        ("apply_entitlement", s.apply),
    ):
        monkeypatch.setattr(f"app.services.billing_service.{name}", mock)
    return s


def _attribute(seams, pubkey: str = PUBKEY):
    """Sync wrapper — the suite has no pytest-asyncio; see tests/test_manual_quota.py."""
    return asyncio.run(
        attribute_unresolved_subscription(
            seams.db,
            subscription_id=SUBSCRIPTION_ID,
            pubkey=pubkey,
            acting_pubkey=ADMIN,
        )
    )


def _dismiss(seams):
    return asyncio.run(
        dismiss_unresolved_subscription(
            seams.db, subscription_id=SUBSCRIPTION_ID, acting_pubkey=ADMIN
        )
    )


# ---------------------------------------------------------------------------
# Attributing
# ---------------------------------------------------------------------------
def test_attributing_grants_the_user_what_the_plan_gives(seams):
    outcome = _attribute(seams)

    assert seams.apply.await_args.kwargs == {
        "external_ref": PUBKEY,
        "subscription_id": SUBSCRIPTION_ID,
    }
    assert outcome.applied is True
    assert outcome.pubkey == PUBKEY


def test_the_grant_is_the_automatic_path_not_a_hand_built_row(monkeypatch, seams):
    """The seam removed: a hand-grant that built its own row could disagree with
    what the next webhook for the same subscription produces."""
    monkeypatch.setattr(
        "app.services.billing_service.apply_entitlement", real_apply_entitlement
    )
    set_scheduling = AsyncMock()
    upsert = AsyncMock()
    for name, mock in (
        ("brainstorm_nsec_exists_by_pubkey_on_db", AsyncMock(return_value=True)),
        ("lock_user_for_update_on_db", AsyncMock(return_value=True)),
        ("fetch_subscription", AsyncMock(return_value=_subscription())),
        ("get_billing_plan_on_db", AsyncMock(return_value=_plan())),
        ("is_billing_blocked_on_db", AsyncMock(return_value=False)),
        ("get_scheduling_source_on_db", AsyncMock(return_value="default")),
        (
            "get_scheduling_on_db",
            AsyncMock(return_value=SimpleNamespace(id=PAID_SCHEDULING_ID, enabled=True)),
        ),
        ("set_scheduling_for_pubkey_on_db", set_scheduling),
        ("upsert_user_subscription_on_db", upsert),
    ):
        monkeypatch.setattr(f"app.services.billing_service.{name}", mock)

    outcome = _attribute(seams)

    args, kwargs = set_scheduling.await_args
    assert args[1:] == (PUBKEY, PAID_SCHEDULING_ID)
    # The grant really did come from a payment, so it is billing's to revoke.
    assert kwargs["source"] == "billing"
    assert upsert.await_args.kwargs["granted_scheduling_id"] == PAID_SCHEDULING_ID
    assert outcome.applied is True


def test_the_subscription_is_confirmed_with_flash_before_anything_is_written(
    monkeypatch, seams
):
    """A row we invent is worse than a refusal — 404 rather than a grant."""
    monkeypatch.setattr(
        "app.services.billing_service.apply_entitlement",
        AsyncMock(
            return_value=EntitlementOutcome(
                applied=False, reason=EntitlementReason.UNKNOWN_SUBSCRIPTION
            )
        ),
    )

    with pytest.raises(HTTPException) as refused:
        _attribute(seams)

    assert refused.value.status_code == 404
    seams.settle.assert_not_awaited()


def test_attributing_settles_the_event_so_the_sweep_stops_rechecking_it(seams):
    _attribute(seams)

    settled = seams.settle.await_args.kwargs
    assert settled["subscription_id"] == SUBSCRIPTION_ID
    assert settled["resolution"] == "attributed"
    # Who acted: a hand-granted entitlement is as traceable as an automatic one.
    assert settled["resolved_by"] == ADMIN


def test_a_settled_event_becomes_prunable_like_any_other():
    """The payload's email survives only while the event is unprocessed, which is
    what makes an unattributed signup matchable months later. Settling is what
    hands it back to the ordinary retention window."""
    assert EntitlementReason.ATTRIBUTED in SETTLED_REASONS
    assert EntitlementReason.DISMISSED in SETTLED_REASONS


def test_a_user_who_already_has_a_subscription_is_not_silently_overwritten(seams):
    seams.existing.return_value = SimpleNamespace(
        pubkey=PUBKEY, flash_subscription_id="9f11"
    )

    with pytest.raises(HTTPException) as refused:
        _attribute(seams)

    assert refused.value.status_code == 409
    assert "9f11" in refused.value.detail
    seams.apply.assert_not_awaited()
    seams.settle.assert_not_awaited()


def test_a_subscription_already_attributed_to_someone_else_is_refused(seams):
    seams.holder.return_value = SimpleNamespace(
        pubkey=OTHER, flash_subscription_id=SUBSCRIPTION_ID
    )

    with pytest.raises(HTTPException) as refused:
        _attribute(seams)

    assert refused.value.status_code == 409
    seams.apply.assert_not_awaited()
    seams.settle.assert_not_awaited()


def test_reattributing_to_the_same_person_changes_nothing(seams):
    """Idempotent rather than an error: the admin asked for a state that already
    holds, and a retry after a half-finished first attempt is exactly this."""
    seams.holder.return_value = SimpleNamespace(
        pubkey=PUBKEY, flash_subscription_id=SUBSCRIPTION_ID
    )

    outcome = _attribute(seams)

    seams.apply.assert_not_awaited()
    assert outcome.applied is False
    # "Already theirs" is a different answer from a grant that ran and decided
    # against acting — without this the caller can only say nothing happened,
    # which reads as a failure and is not even true: they hold the tier.
    assert outcome.entitlement_reason is EntitlementReason.ATTRIBUTED
    # Still settled, so a first attempt that granted and then failed to settle
    # is healed by asking again.
    seams.settle.assert_awaited_once()


def test_flash_says_it_belongs_to_a_different_user(seams):
    """Not our record disagreeing — Flash's. Acting anyway moves the wrong tier."""
    seams.apply.return_value = EntitlementOutcome(
        applied=False, reason=EntitlementReason.REFERENCE_MISMATCH
    )

    with pytest.raises(HTTPException) as refused:
        _attribute(seams)

    assert refused.value.status_code == 409


def test_an_unmapped_plan_leaves_the_row_in_the_report(seams):
    """Settling it would hide a payment nobody can act on."""
    seams.apply.return_value = EntitlementOutcome(
        applied=False, reason=EntitlementReason.UNKNOWN_PLAN
    )

    with pytest.raises(HTTPException) as refused:
        _attribute(seams)

    assert refused.value.status_code == 409
    seams.settle.assert_not_awaited()


def test_a_pubkey_nobody_has_registered_is_refused(seams):
    seams.apply.return_value = EntitlementOutcome(
        applied=False, reason=EntitlementReason.UNKNOWN_USER
    )

    with pytest.raises(HTTPException) as refused:
        _attribute(seams)

    assert refused.value.status_code == 404


def test_a_blocked_user_is_recorded_without_being_granted(seams):
    """BLOCKED settles something — the record is written, the policy is not — so
    the event is resolved rather than left circling."""
    seams.apply.return_value = EntitlementOutcome(
        applied=False, reason=EntitlementReason.BLOCKED
    )

    outcome = _attribute(seams)

    assert outcome.applied is False
    # An attribution that grants nothing is a normal answer, so the outcome has
    # to carry *why* — without it the caller can only report the absence.
    assert outcome.entitlement_reason is EntitlementReason.BLOCKED
    seams.settle.assert_awaited_once()


def test_an_unreachable_flash_settles_nothing(seams):
    """Propagated, not swallowed: the caller turns it into a 503, and the row
    stays exactly as it was."""
    seams.apply.side_effect = FlashUnavailable("socket timed out")

    with pytest.raises(FlashUnavailable):
        _attribute(seams)

    seams.settle.assert_not_awaited()


def test_every_refusal_is_a_plain_string(seams):
    """The frontend renders `detail` as a string, never a dict."""
    for reason in (
        EntitlementReason.UNKNOWN_USER,
        EntitlementReason.UNKNOWN_SUBSCRIPTION,
        EntitlementReason.REFERENCE_MISMATCH,
        EntitlementReason.UNKNOWN_PLAN,
        EntitlementReason.BUSY,
        EntitlementReason.NO_REFERENCE,
    ):
        seams.apply.return_value = EntitlementOutcome(applied=False, reason=reason)
        with pytest.raises(HTTPException) as refused:
            _attribute(seams)
        assert isinstance(refused.value.detail, str), reason


# ---------------------------------------------------------------------------
# Dismissing
# ---------------------------------------------------------------------------
def test_dismissing_grants_nothing(seams):
    outcome = _dismiss(seams)

    seams.apply.assert_not_awaited()
    assert outcome.applied is False
    assert outcome.pubkey is None
    assert outcome.resolution is EntitlementReason.DISMISSED


def test_dismissing_settles_the_event_and_says_who_did_it(seams):
    _dismiss(seams)

    settled = seams.settle.await_args.kwargs
    assert settled["subscription_id"] == SUBSCRIPTION_ID
    assert settled["resolution"] == "dismissed"
    assert settled["resolved_by"] == ADMIN


def test_dismissing_something_with_nothing_open_is_refused(seams):
    """Nothing to write off — either the id is wrong or someone already resolved
    it, and both deserve saying so rather than a silent success."""
    seams.settle.return_value = 0

    with pytest.raises(HTTPException) as refused:
        _dismiss(seams)

    assert refused.value.status_code == 404
    seams.db.commit.assert_not_awaited()
