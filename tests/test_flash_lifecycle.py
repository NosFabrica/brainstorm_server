"""What each Flash subscription status does to a user's scheduling policy.

The governing rule: never take something away that we are not certain has been
lost. Uncertainty holds; only a definite ending revokes.

`decide_entitlement` is pure, so the whole table is testable without a database —
which matters, because the cost of getting a row of it wrong is either charging
someone who receives nothing or giving away what people pay for.
"""

import asyncio
from datetime import datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.db_models import SchedulingSource
from app.services.billing_service import EntitlementDecision, decide_entitlement
from app.services.billing_sync_service import revoke_lapsed_entitlements

NOW = datetime(2026, 8, 25, 12, 0, 0)
LATER = NOW + timedelta(days=5)
EARLIER = NOW - timedelta(days=5)
PUBKEY = "a" * 64


def _decide(status: str, *, cancel_effective=None, period_end=None, now=NOW):
    return decide_entitlement(
        status,
        cancel_effective_date=cancel_effective,
        current_period_end=period_end,
        now=now,
    )


# ---------------------------------------------------------------------------
# Paying, or as good as
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("status", ["active", "trial"])
def test_a_current_subscription_grants(status):
    assert _decide(status) is EntitlementDecision.GRANT


def test_a_successful_renewal_keeps_the_policy():
    """`renewed` arrives as an event, but the status Flash reports is still active."""
    assert _decide("active", period_end=LATER) is EntitlementDecision.GRANT


# ---------------------------------------------------------------------------
# Failing, but not yet lost
# ---------------------------------------------------------------------------
def test_a_failed_renewal_holds_while_flash_is_still_retrying():
    """A card hiccup is not a reason to degrade someone. Flash tells us when it
    gives up, by moving them to expired or canceled."""
    assert _decide("past_due") is EntitlementDecision.HOLD


def test_a_failed_renewal_holds_even_past_the_period_end():
    """Dunning runs past the paid period by design — that IS the grace."""
    assert _decide("past_due", period_end=EARLIER) is EntitlementDecision.HOLD


# ---------------------------------------------------------------------------
# Definitely over
# ---------------------------------------------------------------------------
def test_an_expired_subscription_revokes():
    assert _decide("expired") is EntitlementDecision.REVOKE


def test_a_paused_subscription_revokes():
    assert _decide("paused") is EntitlementDecision.REVOKE


# ---------------------------------------------------------------------------
# Cancelled — paid until the end of what was paid for
# ---------------------------------------------------------------------------
def test_a_cancellation_holds_until_the_paid_period_runs_out():
    assert _decide("canceled", cancel_effective=LATER) is EntitlementDecision.HOLD


def test_a_cancellation_revokes_once_that_date_has_passed():
    assert _decide("canceled", cancel_effective=EARLIER) is EntitlementDecision.REVOKE


def test_a_cancellation_falls_back_to_the_period_end_when_flash_gives_no_date():
    assert _decide("canceled", period_end=LATER) is EntitlementDecision.HOLD
    assert _decide("canceled", period_end=EARLIER) is EntitlementDecision.REVOKE


def test_a_cancellation_with_no_dates_at_all_revokes():
    """Flash words `canceled` in the past tense — "ended by the subscriber or by
    you". A date is what defers it; with none, it has already happened. Holding
    would mean holding forever."""
    assert _decide("canceled") is EntitlementDecision.REVOKE


# ---------------------------------------------------------------------------
# Statuses we have never seen
# ---------------------------------------------------------------------------
def test_an_unrecognised_status_changes_nothing():
    """Flash documents the set as open. A new value must not be read as an ending."""
    assert _decide("subscription.dormant") is EntitlementDecision.HOLD


def test_an_empty_status_changes_nothing():
    assert _decide("") is EntitlementDecision.HOLD


# ---------------------------------------------------------------------------
# The lapse sweep — the same rule, without an event to prompt it
# ---------------------------------------------------------------------------
@pytest.fixture
def sweep(monkeypatch):
    seams = SimpleNamespace(
        db=AsyncMock(),
        candidates=AsyncMock(return_value=[]),
        source=AsyncMock(return_value=SchedulingSource.BILLING.value),
        set_scheduling=AsyncMock(),
        clear=AsyncMock(),
    )
    for name, mock in (
        ("select_entitlement_candidates_on_db", seams.candidates),
        ("get_scheduling_source_on_db", seams.source),
        ("set_scheduling_for_pubkey_on_db", seams.set_scheduling),
        ("clear_granted_scheduling_on_db", seams.clear),
    ):
        monkeypatch.setattr(f"app.services.billing_sync_service.{name}", mock)
    return seams


def _lapsed_row(pubkey=PUBKEY, granted=7, status="canceled", ends=EARLIER):
    """A subscription still holding a policy. Defaults to one that has ended."""
    return SimpleNamespace(
        pubkey=pubkey,
        granted_scheduling_id=granted,
        flash_status=status,
        cancel_effective_date=ends,
        current_period_end=ends,
    )


def test_a_period_that_ran_out_loses_the_policy_with_no_event(sweep):
    sweep.candidates.return_value = [_lapsed_row()]

    revoked = asyncio.run(revoke_lapsed_entitlements(sweep.db, now=NOW))

    assert revoked == 1
    sweep.set_scheduling.assert_awaited_once()
    args, kwargs = sweep.set_scheduling.await_args
    assert args[1] == PUBKEY
    assert args[2] is None  # back to the default policy
    assert kwargs["source"] == SchedulingSource.DEFAULT.value


def test_revocation_leaves_them_indistinguishable_from_a_user_who_never_paid(sweep):
    sweep.candidates.return_value = [_lapsed_row()]

    asyncio.run(revoke_lapsed_entitlements(sweep.db, now=NOW))

    # No lingering grant on the subscription record either.
    sweep.clear.assert_awaited_once()


def test_an_admin_grant_survives_the_sweep(sweep):
    sweep.candidates.return_value = [_lapsed_row()]
    sweep.source.return_value = SchedulingSource.ADMIN.value

    revoked = asyncio.run(revoke_lapsed_entitlements(sweep.db, now=NOW))

    assert revoked == 0
    sweep.set_scheduling.assert_not_awaited()


def test_the_sweep_commits_each_revocation_as_it_makes_it(sweep):
    """Idempotent work, so partial progress surviving a crash beats an
    all-or-nothing batch losing every revocation it had already made."""
    sweep.candidates.return_value = [_lapsed_row("a" * 64), _lapsed_row("b" * 64)]

    asyncio.run(revoke_lapsed_entitlements(sweep.db, now=NOW))

    assert sweep.set_scheduling.await_count == 2
    assert sweep.db.commit.await_count == 2


def test_a_subscription_still_in_dunning_is_left_alone_by_the_sweep(sweep):
    """Locally we cannot tell "renewal failed" from "renewal succeeded and we
    missed the event". Revoking on that ambiguity cuts off someone paying."""
    sweep.candidates.return_value = [_lapsed_row(status="past_due")]

    revoked = asyncio.run(revoke_lapsed_entitlements(sweep.db, now=NOW))

    assert revoked == 0
    sweep.set_scheduling.assert_not_awaited()


def test_a_still_current_subscription_is_left_alone_by_the_sweep(sweep):
    sweep.candidates.return_value = [_lapsed_row(status="active", ends=LATER)]

    assert asyncio.run(revoke_lapsed_entitlements(sweep.db, now=NOW)) == 0


def test_the_sweep_and_the_event_path_apply_the_same_rule(sweep):
    """One judge, so the two cannot drift — the hazard app/repos/CLAUDE.md
    describes for the tier predicates."""
    ended = _lapsed_row(status="expired", ends=None)
    sweep.candidates.return_value = [ended]

    assert asyncio.run(revoke_lapsed_entitlements(sweep.db, now=NOW)) == 1
    assert _decide("expired") is EntitlementDecision.REVOKE


def test_nothing_lapsed_writes_nothing(sweep):
    asyncio.run(revoke_lapsed_entitlements(sweep.db, now=NOW))

    sweep.set_scheduling.assert_not_awaited()
    sweep.db.commit.assert_not_awaited()


# ---------------------------------------------------------------------------
# When the sweep runs
# ---------------------------------------------------------------------------
def test_the_sweep_follows_flash_rather_than_needing_its_own_switch(monkeypatch):
    """Configuring Flash without the sweep is not a conservative state: tiers
    get granted and never taken back."""
    from app.core.config import settings

    monkeypatch.setattr(settings, "flash_enabled", True)
    monkeypatch.setattr(settings, "billing_sync_enabled", None)

    assert settings.billing_sync_active is True


def test_the_sweep_stays_off_where_flash_is_not_configured(monkeypatch):
    from app.core.config import settings

    monkeypatch.setattr(settings, "flash_enabled", False)
    monkeypatch.setattr(settings, "billing_sync_enabled", None)

    assert settings.billing_sync_active is False


def test_the_sweep_can_be_stopped_without_unmounting_the_webhook(monkeypatch):
    """`flash_enabled=false` would 404 Flash's deliveries until it gave up on
    them, so stopping the sweep needs its own lever."""
    from app.core.config import settings

    monkeypatch.setattr(settings, "flash_enabled", True)
    monkeypatch.setattr(settings, "billing_sync_enabled", False)

    assert settings.billing_sync_active is False


def test_the_abandon_window_is_shorter_than_the_sweep_that_applies_it():
    """Both were 21600s, so a row became eligible at the exact moment the cycle
    evaluating it ran — and on staging it lost that race by milliseconds and
    waited a further six hours.

    The window is a *minimum* age, and the sweep can only act on cycle
    boundaries, so anything >= the interval makes the first eligible cycle a
    coin flip. Shorter by a margin makes it deterministic: by the time a cycle
    looks, the row has been eligible for a while."""
    from app.core.config import settings

    assert (
        settings.billing_abandon_pending_after_seconds
        < settings.billing_sync_interval_seconds
    ), "the abandon window must be shorter than the cycle that applies it"
