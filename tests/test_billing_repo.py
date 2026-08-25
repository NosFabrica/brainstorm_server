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
    from app.repos import billing_repo

    built = []

    async def _capture(db, statement, name):
        built.append(statement)
        return SimpleNamespace(
            scalar_one_or_none=lambda: None,
            scalars=lambda: SimpleNamespace(all=lambda: []),
            all=lambda: [],
            rowcount=0,
        )

    monkeypatch.setattr(billing_repo, "execute_db_statement", _capture)

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
        "now": NOW,
        "stale_after": timedelta(minutes=5),
        "older_than": NOW,
        "max_attempts": 5,
        "limit": 10,
        "known": ["active"],
        "admin_held": False,
        "since": NOW,
        "until": NOW,
    }

    functions = [
        (name, fn)
        for name, fn in vars(billing_repo).items()
        # Defined here, not merely imported — otherwise this quietly tests
        # someone else's module.
        if name.endswith("_on_db")
        and inspect.iscoroutinefunction(fn)
        and fn.__module__ == billing_repo.__name__
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
