"""The payload fallback: what applies when Flash itself cannot be read.

Only the endings carry their own answer — `expired` revokes, `canceled` and
`past_due` record the status for the sweeps. `activated`/`renewed` never
fall back: their payloads omit the period boundaries, which is the whole
reason entitlement re-fetches.
"""

import asyncio
from datetime import datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.services import billing_service as svc

PUBKEY = "a" * 64


@pytest.fixture
def seams(monkeypatch):
    stubs = SimpleNamespace(
        lock=AsyncMock(return_value=True),
        existing=AsyncMock(
            return_value=SimpleNamespace(
                granted_scheduling_id=7,
                cancel_effective_date=None,
                current_period_end=datetime(2026, 9, 25, 12, 0, 0),
            )
        ),
        source=AsyncMock(return_value="billing"),
        set_policy=AsyncMock(),
        clear_grant=AsyncMock(),
        set_status=AsyncMock(),
    )
    for name, mock in (
        ("lock_user_for_update_on_db", stubs.lock),
        ("get_user_subscription_on_db", stubs.existing),
        ("get_scheduling_source_on_db", stubs.source),
        ("set_scheduling_for_pubkey_on_db", stubs.set_policy),
        ("clear_granted_scheduling_on_db", stubs.clear_grant),
        ("update_flash_status_on_db", stubs.set_status),
    ):
        monkeypatch.setattr(svc, name, mock)
    return stubs


def _run(event, external_ref=PUBKEY):
    return asyncio.run(
        svc.apply_payload_fallback(
            AsyncMock(), event=event, external_ref=external_ref
        )
    )


def test_expired_revokes_without_asking_flash(seams):
    assert _run("subscription.expired") is True
    seams.set_policy.assert_awaited_once()
    assert seams.set_policy.await_args.args[2] is None  # back to the default
    seams.clear_grant.assert_awaited_once()
    assert seams.set_status.await_args.args[2] == "expired"


def test_expired_leaves_an_admin_assignment_alone(seams):
    seams.source.return_value = "admin"
    assert _run("subscription.expired") is True
    seams.set_policy.assert_not_awaited()
    # The status is still recorded — the record is what Flash said.
    assert seams.set_status.await_args.args[2] == "expired"


@pytest.mark.parametrize(
    ("event", "status"),
    [
        ("subscription.canceled", "canceled"),
        ("subscription.past_due", "past_due"),
    ],
)
def test_endings_record_the_status_but_keep_the_policy(seams, event, status):
    assert _run(event) is True
    seams.set_policy.assert_not_awaited()
    assert seams.set_status.await_args.args[2] == status


def test_a_fallback_cancel_materializes_when_the_entitlement_ends(seams):
    """PRD: `canceled` → set the date. The payload has no effective date, so
    the paid-through boundary is the period end already on record."""
    _run("subscription.canceled")
    kwargs = seams.set_status.await_args.kwargs
    assert kwargs["cancel_effective_date"] == datetime(2026, 9, 25, 12, 0, 0)


def test_a_fallback_cancel_never_overwrites_a_known_date(seams):
    seams.existing.return_value = SimpleNamespace(
        granted_scheduling_id=7,
        cancel_effective_date=datetime(2026, 9, 1, 0, 0, 0),
        current_period_end=datetime(2026, 9, 25, 12, 0, 0),
    )
    _run("subscription.canceled")
    assert seams.set_status.await_args.kwargs["cancel_effective_date"] is None


@pytest.mark.parametrize(
    "event",
    ["subscription.activated", "subscription.renewed", "credential.check", "new.thing"],
)
def test_payments_and_unknowns_never_fall_back(seams, event):
    """The missing dates are the point — inferring entitlement here is the
    lost-update bug the re-fetch design exists to prevent."""
    assert _run(event) is False
    seams.set_policy.assert_not_awaited()
    seams.set_status.assert_not_awaited()


def test_a_subscriber_we_hold_no_record_of_is_left_alone(seams):
    seams.existing.return_value = None
    assert _run("subscription.expired") is False
    seams.set_policy.assert_not_awaited()


def test_a_busy_subscriber_is_left_to_the_live_worker(seams):
    seams.lock.return_value = False
    assert _run("subscription.expired") is False
    seams.set_status.assert_not_awaited()
