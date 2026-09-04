"""Turning a Flash activation into a paid scheduling policy.

Entitlement is the scheduling assignment; the subscription record only explains
it. So these assert *which policy a pubkey ends up on*, and that nothing moves a
user we can't confidently account for.

Repos and the Flash lookup are mocked — this is orchestration, and the point is
which calls happen (and which don't), not persistence.
"""

import asyncio
from dataclasses import replace
from datetime import datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi import HTTPException

from app.core.flash import FlashPricing, FlashSubscription, FlashUnavailable
from app.services.billing_service import (
    EntitlementOutcome,
    EntitlementReason,
    apply_entitlement,
)

PUBKEY = "a" * 64
PAID_SCHEDULING_ID = 7
SUBSCRIPTION_ID = "7d3b"


def _subscription(
    status: str = "active",
    ref: str = PUBKEY,
    pricing: FlashPricing | None = FlashPricing(
        amount_minor=200, currency="USD", billing_interval="monthly"
    ),
    **overrides,
) -> FlashSubscription:
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
        pricing=pricing,
        **overrides,
    )


def _plan(scheduling_id: int = PAID_SCHEDULING_ID) -> SimpleNamespace:
    return SimpleNamespace(
        id=1,
        flash_service_id="9c1e",
        flash_plan_id="4f2a",
        billing_period_unit="month",
        billing_period_count=1,
        sort_order=0,
        blurb=None,
        includes=None,
        excludes=None,
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
        source=AsyncMock(return_value="default"),
        policy=AsyncMock(return_value=SimpleNamespace(id=PAID_SCHEDULING_ID, enabled=True)),
        existing=AsyncMock(return_value=None),
        lock=AsyncMock(return_value=True),
        set_scheduling=AsyncMock(),
        upsert=AsyncMock(),
    )
    for name, mock in (
        ("brainstorm_nsec_exists_by_pubkey_on_db", seams.user_exists),
        ("fetch_subscription", seams.fetch),
        ("get_billing_plan_on_db", seams.plan),
        ("is_billing_blocked_on_db", seams.blocked),
        ("get_scheduling_source_on_db", seams.source),
        ("get_scheduling_on_db", seams.policy),
        ("get_user_subscription_on_db", seams.existing),
        ("lock_user_for_update_on_db", seams.lock),
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
    assert recorded["subscription"].current_period_end == datetime(2026, 9, 20, 14, 3, 11)
    assert recorded["subscription"].status == "active"


def test_flash_status_is_recorded_verbatim_not_translated(billing):
    billing.fetch.return_value = _subscription(status="trial")

    _apply(billing)

    assert billing.upsert.await_args.kwargs["subscription"].status == "trial"


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
    assert outcome.reason is EntitlementReason.UNKNOWN_USER
    billing.set_scheduling.assert_not_awaited()


def test_a_missing_reference_moves_nobody(billing):
    outcome = _apply(billing, ref=None)

    assert outcome.applied is False
    assert outcome.reason is EntitlementReason.NO_REFERENCE
    billing.set_scheduling.assert_not_awaited()
    billing.fetch.assert_not_awaited()


def test_an_unrecognised_plan_moves_nobody(billing):
    billing.plan.return_value = None

    outcome = _apply(billing)

    assert outcome.applied is False
    assert outcome.reason is EntitlementReason.UNKNOWN_PLAN
    billing.set_scheduling.assert_not_awaited()


def test_an_unreachable_flash_moves_nobody(billing):
    """Propagated rather than swallowed — the caller decides what it means.
    The webhook path logs and moves on; the reconcile loop records and may stop."""
    billing.fetch.side_effect = FlashUnavailable("vault down")

    with pytest.raises(FlashUnavailable):
        _apply(billing)

    billing.set_scheduling.assert_not_awaited()
    billing.db.commit.assert_not_awaited()


def test_a_subscription_belonging_to_someone_else_moves_nobody(billing):
    """Both fields come from Flash, so disagreement means something is wrong —
    and acting on it would move the wrong person's tier."""
    billing.fetch.return_value = _subscription(ref="b" * 64)

    outcome = _apply(billing)

    assert outcome.reason is EntitlementReason.REFERENCE_MISMATCH
    billing.set_scheduling.assert_not_awaited()


def test_a_subscription_that_names_nobody_moves_nobody(billing):
    """The guide grants only on a subscription that "carries the expected ref",
    and one carrying none does not. It used to slip past the mismatch branch and
    be granted, which was safe only while every id came from Flash's own
    webhooks — the moment a browser can supply one, any signed-in caller could
    claim an unattributed signup by quoting its id."""
    billing.fetch.return_value = _subscription(ref=None)

    outcome = _apply(billing)

    assert outcome.applied is False
    assert outcome.reason is EntitlementReason.NO_REFERENCE
    billing.set_scheduling.assert_not_awaited()
    billing.upsert.assert_not_awaited()


def test_an_id_for_a_superseded_subscription_is_decided_from_the_current_one(billing):
    """A re-subscribe leaves two rows under one ref. An old redirect replayed, or
    a late `expired` for the previous subscription, names the one that no longer
    decides anything — deciding from it alone would revoke someone who is paying."""
    stale = replace(_subscription(status="canceled"), id="old")
    current = _subscription(status="active")
    billing.fetch.side_effect = [stale, current]
    billing.existing.return_value = SimpleNamespace(
        flash_subscription_id=SUBSCRIPTION_ID, granted_scheduling_id=None
    )

    outcome = asyncio.run(
        apply_entitlement(billing.db, external_ref=PUBKEY, subscription_id="old")
    )

    assert outcome.reason is EntitlementReason.GRANTED
    assert billing.fetch.await_args_list[1].kwargs == {"ref": PUBKEY}
    assert billing.upsert.await_args.kwargs["subscription"] is current


def test_an_id_matching_the_row_on_file_is_read_once(billing):
    billing.existing.return_value = SimpleNamespace(
        flash_subscription_id=SUBSCRIPTION_ID, granted_scheduling_id=None
    )

    _apply(billing)

    assert billing.fetch.await_count == 1


def test_an_operator_attributing_by_hand_is_the_one_way_that_grants(billing):
    """An unresolved signup names nobody by definition, so attribution has to be
    able to grant one — and it is the only caller that can, because it is the
    only one where a human has decided whose payment it is."""
    billing.fetch.return_value = _subscription(ref=None)

    outcome = asyncio.run(
        apply_entitlement(
            billing.db,
            external_ref=PUBKEY,
            subscription_id=SUBSCRIPTION_ID,
            allow_unreferenced=True,
        )
    )

    assert outcome.applied is True
    assert outcome.reason is EntitlementReason.GRANTED


def test_a_subscription_that_does_not_entitle_moves_nobody(billing):
    billing.fetch.return_value = _subscription(status="pending")

    outcome = _apply(billing)

    assert outcome.applied is False
    billing.set_scheduling.assert_not_awaited()


def test_a_lapsed_status_keeps_the_record_of_what_was_granted(billing):
    """Blanking it would leave slice 03 with nothing to take back, while the
    user sits on a tier no record accounts for."""
    billing.existing.return_value = SimpleNamespace(
        flash_subscription_id=SUBSCRIPTION_ID, granted_scheduling_id=PAID_SCHEDULING_ID
    )
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


def test_the_subscriber_is_locked_before_flash_is_read(billing):
    """Fetching first and locking second lets two handlers both read, then
    serialise — and whichever read EARLIER writes last, so the older view wins."""
    order = []
    billing.lock.side_effect = lambda *a, **k: order.append("lock") or True
    billing.fetch.side_effect = lambda *a, **k: order.append("fetch") or _subscription()

    _apply(billing)

    assert order == ["lock", "fetch"]


def test_a_live_delivery_is_never_made_to_wait_by_background_work(billing):
    """Flash allows ten seconds to acknowledge, and the lock is held across a
    Flash read — so the deadline-free side is the one that yields."""
    billing.lock.return_value = False

    outcome = asyncio.run(
        apply_entitlement(
            billing.db,
            external_ref=PUBKEY,
            subscription_id=SUBSCRIPTION_ID,
            yield_if_busy=True,
        )
    )

    assert outcome.reason is EntitlementReason.BUSY
    billing.fetch.assert_not_awaited()


# ---------------------------------------------------------------------------
# Admin decisions vs paying for something
# ---------------------------------------------------------------------------
def test_a_paying_user_is_granted_even_where_an_admin_last_set_the_policy(billing):
    """An admin assignment stops billing taking a tier AWAY (slice 03); it must
    never stop someone receiving what they are being charged for."""
    outcome = _apply(billing)

    assert outcome.applied is True
    billing.set_scheduling.assert_awaited_once()


def test_granting_to_a_comped_user_does_not_erase_the_comp(billing):
    """Overwriting the source would let the next `expired` revoke them — the
    comp would die by a delayed path rather than an admin decision."""
    billing.source.return_value = "admin"

    _apply(billing)

    assert billing.set_scheduling.await_args.kwargs["source"] == "admin"
    assert billing.set_scheduling.await_args.args[2] == PAID_SCHEDULING_ID


def test_granting_hands_the_policy_back_to_billing(billing):
    _apply(billing)

    assert billing.set_scheduling.await_args.kwargs["source"] == "billing"


# ---------------------------------------------------------------------------
# Revocation through the event path
# ---------------------------------------------------------------------------
def test_an_ended_subscription_loses_the_policy(billing):
    billing.fetch.return_value = _subscription(status="expired")

    outcome = _apply(billing)

    assert outcome.reason is EntitlementReason.REVOKED
    args, kwargs = billing.set_scheduling.await_args
    assert args[2] is None
    assert kwargs["source"] == "default"


def test_revocation_clears_the_recorded_grant(billing):
    billing.existing.return_value = SimpleNamespace(
        flash_subscription_id=SUBSCRIPTION_ID, granted_scheduling_id=PAID_SCHEDULING_ID
    )
    billing.fetch.return_value = _subscription(status="expired")

    _apply(billing)

    assert billing.upsert.await_args.kwargs["granted_scheduling_id"] is None


def test_an_admin_grant_is_not_revoked_by_an_ended_subscription(billing):
    billing.source.return_value = "admin"
    billing.fetch.return_value = _subscription(status="expired")

    outcome = _apply(billing)

    assert outcome.reason is EntitlementReason.ADMIN_OVERRIDE
    billing.set_scheduling.assert_not_awaited()


# ---------------------------------------------------------------------------
# A plan withdrawn from sale
# ---------------------------------------------------------------------------
def test_a_subscriber_on_a_retired_plan_is_still_granted_on_renewal(billing):
    """Retiring a plan stops it being sold. It does not strand whoever bought
    it while it was — they are still paying, and still owed what they bought."""
    billing.plan.return_value = _plan()
    billing.plan.return_value.is_active = False

    outcome = _apply(billing)

    assert outcome.reason is EntitlementReason.GRANTED
    assert billing.set_scheduling.await_args.args[2] == PAID_SCHEDULING_ID


def test_a_subscriber_on_a_retired_plan_is_still_revoked_when_it_ends(billing):
    """The half that used to be impossible: nothing downstream of the plan
    lookup consults `is_active`, so an ending is applied like any other."""
    billing.plan.return_value = _plan()
    billing.plan.return_value.is_active = False
    billing.fetch.return_value = _subscription(status="expired")

    outcome = _apply(billing)

    assert outcome.reason is EntitlementReason.REVOKED
    assert billing.set_scheduling.await_args.args[2] is None


def test_a_failed_renewal_leaves_the_policy_alone(billing):
    billing.fetch.return_value = _subscription(status="past_due")

    outcome = _apply(billing)

    assert outcome.reason is EntitlementReason.HELD
    billing.set_scheduling.assert_not_awaited()


# ---------------------------------------------------------------------------
# Blocking
# ---------------------------------------------------------------------------
def test_a_blocked_user_is_never_granted_even_while_paying(billing):
    billing.blocked.return_value = True

    outcome = _apply(billing)

    assert outcome.applied is False
    assert outcome.reason is EntitlementReason.BLOCKED
    billing.set_scheduling.assert_not_awaited()


def test_a_blocked_user_still_has_their_subscription_recorded(billing):
    """They are still being charged, so support must be able to see it — and
    they must still be able to cancel and leave."""
    billing.blocked.return_value = True

    _apply(billing)

    billing.upsert.assert_awaited_once()
    assert billing.upsert.await_args.kwargs["subscription"].status == "active"


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


# ---------------------------------------------------------------------------
# Creating the missing mapping is the whole fix, not the first half of it
# ---------------------------------------------------------------------------
PLAN_VALUES = {
    "flash_service_id": "9c1e",
    "flash_plan_id": "4f2a",
    "billing_period_unit": "month",
    "billing_period_count": 1,
    "sort_order": 0,
    "scheduling_id": PAID_SCHEDULING_ID,
    "amount_minor": 200,
    "currency": "USD",
    "is_active": True,
}


@pytest.fixture
def new_plan(monkeypatch):
    calls: list[str] = []
    seams = SimpleNamespace(
        db=AsyncMock(),
        calls=calls,
        exists=AsyncMock(return_value=True),
        insert=AsyncMock(return_value=_plan()),
        reset=AsyncMock(return_value=2),
        by_flash_ids=AsyncMock(return_value=None),
    )
    seams.db.commit.side_effect = lambda: calls.append("commit")
    seams.reset.side_effect = lambda *a, **k: calls.append("reset") or 2
    for name, mock in (
        ("scheduling_exists_on_db", seams.exists),
        ("insert_billing_plan_on_db", seams.insert),
        ("reset_events_awaiting_plan_on_db", seams.reset),
        ("get_billing_plan_on_db", seams.by_flash_ids),
    ):
        monkeypatch.setattr(f"app.services.billing_service.{name}", mock)
    return seams


def _create(new_plan):
    from app.services.billing_service import create_billing_plan

    return asyncio.run(create_billing_plan(new_plan.db, dict(PLAN_VALUES)))


def test_mapping_a_plan_frees_the_events_that_were_waiting_on_it(new_plan):
    """Otherwise the admin has made the mapping and still has an unentitled
    paying subscriber, with nothing saying a second step remains."""
    _create(new_plan)

    new_plan.reset.assert_awaited_once()
    kwargs = new_plan.reset.await_args.kwargs
    assert kwargs["flash_service_id"] == "9c1e"
    assert kwargs["flash_plan_id"] == "4f2a"
    assert kwargs["error"] == EntitlementReason.UNKNOWN_PLAN.value


def test_the_mapping_and_the_events_it_heals_commit_together(new_plan):
    """An event made replayable against a plan that never landed would fail
    identically on the next pass."""
    _create(new_plan)

    assert new_plan.calls == ["reset", "commit"]


def test_a_plan_that_cannot_be_created_frees_nothing(new_plan):
    """No mapping, so nothing is waiting on one."""
    new_plan.exists.return_value = False

    with pytest.raises(HTTPException):
        _create(new_plan)

    new_plan.reset.assert_not_awaited()
    new_plan.db.commit.assert_not_awaited()


def test_flash_ids_already_mapped_are_refused_in_words(new_plan):
    """The pair is unique in the schema; an admin retyping a live pair should
    read why rather than a constraint violation."""
    new_plan.by_flash_ids.return_value = _plan()

    with pytest.raises(HTTPException) as caught:
        _create(new_plan)

    assert caught.value.status_code == 409
    assert isinstance(caught.value.detail, str)


# ---------------------------------------------------------------------------
# Correcting a mapping — the only repair mechanism there is
# ---------------------------------------------------------------------------
@pytest.fixture
def edit_plan(new_plan, monkeypatch):
    new_plan.current = AsyncMock(return_value=_plan())
    new_plan.update = AsyncMock(return_value=_plan())
    new_plan.subscribers = AsyncMock(return_value=0)
    for name, mock in (
        ("get_billing_plan_by_id_on_db", new_plan.current),
        ("update_billing_plan_on_db", new_plan.update),
        ("count_subscriptions_for_plan_on_db", new_plan.subscribers),
    ):
        monkeypatch.setattr(f"app.services.billing_service.{name}", mock)
    return new_plan


def _update(edit_plan, values):
    from app.services.billing_service import update_billing_plan

    return asyncio.run(update_billing_plan(edit_plan.db, 1, values))


def test_a_price_is_editable_whoever_is_on_the_plan(edit_plan):
    """Nothing can verify a transcribed price, so correcting one by hand is the
    only repair there is — and it must not depend on nobody having bought it."""
    edit_plan.subscribers.return_value = 3

    _update(edit_plan, {"amount_minor": 1000})

    assert edit_plan.update.await_args.args[2] == {"amount_minor": 1000}


def test_flash_ids_are_editable_while_nobody_has_bought_the_plan(edit_plan):
    """The common case: a row that was misconfigured and never sold."""
    _update(edit_plan, {"flash_plan_id": "beef"})

    assert edit_plan.update.await_args.args[2] == {"flash_plan_id": "beef"}


def test_rewriting_flash_ids_under_a_subscriber_is_refused_with_the_way_out(
    edit_plan,
):
    """It would retroactively change what those people bought."""
    edit_plan.subscribers.return_value = 2

    with pytest.raises(HTTPException) as caught:
        _update(edit_plan, {"flash_service_id": "other"})

    assert caught.value.status_code == 409
    assert "deactivate" in caught.value.detail
    edit_plan.update.assert_not_awaited()


def test_resending_the_same_flash_ids_is_not_a_re_identification(edit_plan):
    """A form that sends the ids back unchanged is not a rewrite, and must not
    be refused for a plan people are on."""
    edit_plan.subscribers.return_value = 2

    _update(edit_plan, {"flash_service_id": "9c1e", "flash_plan_id": "4f2a"})

    edit_plan.update.assert_awaited_once()
    edit_plan.reset.assert_not_awaited()


def test_correcting_a_flash_id_frees_the_events_that_were_waiting_on_it(edit_plan):
    """Same reason creating a mapping does: those events already spent their
    attempts, so the typo fix would otherwise leave a subscriber unentitled."""
    _update(edit_plan, {"flash_plan_id": "beef"})

    kwargs = edit_plan.reset.await_args.kwargs
    assert kwargs["flash_plan_id"] == "beef"
    assert kwargs["error"] == EntitlementReason.UNKNOWN_PLAN.value


def test_editing_a_plan_that_does_not_exist_is_a_404(edit_plan):
    edit_plan.current.return_value = None

    with pytest.raises(HTTPException) as caught:
        _update(edit_plan, {"sort_order": 3})

    assert caught.value.status_code == 404
