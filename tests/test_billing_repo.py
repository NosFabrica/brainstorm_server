"""Every billing query is built at least once, against the real models.

A statement naming a column the model does not have raises only when it is first
built — and every service test mocks this layer, so nothing would catch it until
production. That is not hypothetical: `FlashWebhookEvent.received_at` shipped in
one slice against a column removed in another, and the suite stayed green.

This validates model attributes, not that the columns exist in Postgres — a
missing migration still passes here.
"""

import asyncio
from datetime import datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

from app.core.flash import FlashSubscription
from app.repos.user_subscription_repo import AbandonRule

NOW = datetime(2026, 8, 25, 12, 0, 0)
PUBKEY = "a" * 64


# ---------------------------------------------------------------------------
# The queries actually reference columns that exist
# ---------------------------------------------------------------------------
def test_every_query_builds_against_the_real_models(monkeypatch):
    """A statement naming a column the model doesn't have raises only when it is
    first built — and the service tests all mock this layer, so nothing would
    catch it until production. This builds every one of them.
    """
    import inspect
    from app.repos import (
        billing_plan_repo,
        flash_webhook_event_repo,
        user_subscription_repo,
    )

    modules = (billing_plan_repo, user_subscription_repo, flash_webhook_event_repo)
    built = []

    async def _capture(db, statement, name):
        built.append(statement)
        return SimpleNamespace(
            scalar_one_or_none=lambda: None,
            scalar_one=lambda: 0,
            scalars=lambda: SimpleNamespace(all=lambda: [], first=lambda: None),
            all=lambda: [],
            rowcount=0,
        )

    for module in modules:
        monkeypatch.setattr(module, "execute_db_statement", _capture)

    sample = {
        "db": AsyncMock(),
        "pubkey": PUBKEY,
        "event": "subscription.activated",
        "event_id": 1,
        "delivery_timestamp": 1700000000,
        "event_timestamp": NOW,
        "subscription_id": "7d3b",
        "payload": {},
        "dedupe_key": "k",
        "claimed_at": NOW,
        "skip_locked": False,
        "flash_service_id": "9c1e",
        "flash_plan_id": "4f2a",
        "flash_subscription_id": "7d3b",
        "flash_subscriber_id": "a91c",
        "billing_plan_id": 1,
        "granted_scheduling_id": 7,
        "flash_status": "active",
        "current_period_start": NOW,
        "current_period_end": NOW,
        "next_billing_date": NOW,
        "trial_end_date": None,
        "cancel_effective_date": None,
        "reason": "because",
        "resolution": "attributed",
        "resolved_by": PUBKEY,
        "error": "unknown_plan",
        "now": NOW,
        "stale_after": timedelta(minutes=5),
        "abandoned": AbandonRule(
            after=timedelta(hours=24), error="unknown_subscription"
        ),
        "older_than": NOW,
        "max_attempts": 5,
        "limit": 10,
        "known": ["active"],
        "admin_held": False,
        "since": NOW,
        "until": NOW,
        "only_active": True,
        "subscription": FlashSubscription(
            id="7d3b",
            status="active",
            ref=PUBKEY,
            subscriber_id="a91c",
            service_id="9c1e",
            plan_id="4f2a",
            current_period_start=NOW,
            current_period_end=NOW,
            next_billing_date=NOW,
            trial_end_date=None,
            cancel_effective_date=None,
        ),
        "plan_id": 1,
        "scheduling_id": 7,
        "status": "canceled",
        "values": {},
        "amount_minor": 200,
        "currency": "USD",
        "billing_period_unit": "month",
        "billing_period_count": 1,
        "sort_order": 0,
        "blurb": "The cheap one",
        "includes": ["faster recalculation"],
        "excludes": [],
        "is_active": True,
    }

    functions = [
        (name, fn)
        for module in modules
        for name, fn in vars(module).items()
        # Defined here, not merely imported — otherwise this quietly tests
        # someone else's module.
        if name.endswith("_on_db")
        and inspect.iscoroutinefunction(fn)
        and fn.__module__ == module.__name__
    ]
    assert functions, "no repo functions found — did the module move?"

    for name, fn in functions:
        params = [p for p in inspect.signature(fn).parameters if p != "db"]
        missing = [p for p in params if p not in sample]
        assert not missing, (
            f"{name} takes {missing}, which this test has no sample value for. "
            "Add one to `sample` rather than letting the parameter go untested."
        )
        asyncio.run(fn(sample["db"], **{k: sample[k] for k in params}))

    assert built, "no statements were built"


# ---------------------------------------------------------------------------
# `is_active` means sellable, and nothing else
# ---------------------------------------------------------------------------
def _built(monkeypatch, module, fn, **kwargs):
    """One repo statement, built without a database."""
    captured = []

    async def _capture(db, statement, name):
        captured.append(statement)
        return SimpleNamespace(
            scalar_one_or_none=lambda: None,
            scalar_one=lambda: 0,
            scalars=lambda: SimpleNamespace(all=lambda: [], first=lambda: None),
            all=lambda: [],
            rowcount=0,
        )

    monkeypatch.setattr(module, "execute_db_statement", _capture)
    asyncio.run(fn(AsyncMock(), **kwargs))
    return captured[0]


def _sql(statement) -> str:
    """The SQL with its bind parameters inlined — JSON keys are bound values, so
    without this the payload paths a query reads are invisible to a test."""
    from sqlalchemy.dialects import postgresql

    return str(
        statement.compile(
            dialect=postgresql.dialect(), compile_kwargs={"literal_binds": True}
        )
    )


def test_retiring_a_plan_does_not_unmap_the_people_on_it(monkeypatch):
    """The entitlement lookup sits ahead of all status handling, so filtering it
    on `is_active` froze every subscriber on a withdrawn plan — no renewal, and
    no expiry or cancellation either, which is a permanent comp."""
    from app.repos import billing_plan_repo

    statement = _built(
        monkeypatch,
        billing_plan_repo,
        billing_plan_repo.get_billing_plan_on_db,
        flash_service_id="9c1e",
        flash_plan_id="4f2a",
    )

    assert "is_active" not in str(statement.whereclause)


def test_a_retired_plan_is_no_longer_offered_for_sale(monkeypatch):
    """Which is all the flag does."""
    from app.repos import billing_plan_repo

    sellable = _built(
        monkeypatch,
        billing_plan_repo,
        billing_plan_repo.select_billing_plans_on_db,
        only_active=True,
    )

    assert "is_active" in str(sellable.whereclause)


def _unresolved_sql(monkeypatch):
    """Both halves of what was one query, as SQL."""
    from app.repos import flash_webhook_event_repo

    return tuple(
        _sql(_built(monkeypatch, flash_webhook_event_repo, selector, limit=200))
        for selector in (
            flash_webhook_event_repo.select_unresolved_signups_on_db,
            flash_webhook_event_repo.select_unmapped_plan_events_on_db,
        )
    )


def test_the_split_loses_no_event_that_needs_attention(monkeypatch):
    """"Named nobody" and "a plan we never mapped" are two fixes, so they are two
    sections — but only if the predicates are exact complements. Split badly and
    an event needing attention simply stops being reported."""
    signups, plans = _unresolved_sql(monkeypatch)

    # The same unapplied population on both sides...
    for statement in (signups, plans):
        assert "processed_at IS NULL" in statement
        assert "process_error IS NOT NULL" in statement
        # ...divided on the reference, empty counting as absent on both, so no
        # row can land in neither section.
        ref = statement.partition("WHERE")[2]
        assert "'externalRef'" in ref and "IS NULL OR" in ref and "= ''" in ref
    # One side is the negation of the other, not a second positive guess.
    assert "NOT (" not in signups
    assert plans.count("NOT (") == 1


def test_each_section_carries_only_what_its_own_fix_needs(monkeypatch):
    """Attributing a signup acts on the Flash id, which is its only handle;
    mapping a plan needs the service/plan pair. Neither wants the other's."""
    signups, plans = _unresolved_sql(monkeypatch)
    signup_columns = signups.partition("WHERE")[0]
    plan_columns = plans.partition("WHERE")[0]

    assert "flash_subscription_id" in signup_columns
    assert "'serviceId'" not in signup_columns and "'planId'" not in signup_columns

    for column in ("external_ref", "flash_service_id", "flash_plan_id"):
        assert column in plan_columns
    assert "'serviceId'" in plan_columns and "'planId'" in plan_columns


def test_contact_details_are_read_only_for_a_signup_that_named_nobody(monkeypatch):
    """Where there is a reference the subscriber is already ours to look up, and
    copying their email into an operational report is personal data for nothing.
    The section boundary is the guard: the reference-less query is the only one
    that names a personal payload key at all."""
    signups, plans = _unresolved_sql(monkeypatch)

    for personal in ("'email'", "'name'"):
        assert personal in signups
        assert personal not in plans
    assert "subscriber_email" not in plans and "subscriber_name" not in plans


def test_only_the_events_that_were_waiting_on_this_plan_are_freed(monkeypatch):
    """A delivery held up by something else does not get a free extra life."""
    from app.repos import flash_webhook_event_repo

    statement = _built(
        monkeypatch,
        flash_webhook_event_repo,
        flash_webhook_event_repo.reset_events_awaiting_plan_on_db,
        flash_service_id="9c1e",
        flash_plan_id="4f2a",
        error="unknown_plan",
    )
    where = _sql(statement).partition("WHERE")[2]

    assert "'unknown_plan'" in where
    assert "processed_at IS NULL" in where
    assert "'serviceId'" in where and "'planId'" in where


def test_an_unmapped_plan_that_is_never_mapped_stops_replaying(monkeypatch):
    """Freeing events is what plan creation does; the attempt cap is what stops
    the ones nobody ever maps."""
    from app.repos import flash_webhook_event_repo

    statement = _built(
        monkeypatch,
        flash_webhook_event_repo,
        flash_webhook_event_repo.select_abandoned_webhook_events_on_db,
        now=NOW,
        stale_after=timedelta(minutes=5),
        max_attempts=5,
        limit=25,
    )

    assert "attempts <" in str(statement.whereclause)


def test_the_retired_plan_report_names_the_flash_subscription(monkeypatch):
    """Ending one of these is Flash's to do, one subscription at a time — so the
    row has to carry the id the admin view links out on."""
    from app.repos import user_subscription_repo

    statement = _built(
        monkeypatch,
        user_subscription_repo,
        user_subscription_repo.select_retired_plan_subscribers_on_db,
        limit=200,
    )

    assert "flash_subscription_id" in str(statement)
    assert "is_active" in str(statement.whereclause)
