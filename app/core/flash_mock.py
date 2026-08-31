"""In-process stand-in for Flash's subscriptions API.

There is no Flash sandbox — every real test is a real payment — so this is how
the paid paths are exercised locally: `flash_mock_enabled` routes
`fetch_subscription` here, and the LOCAL-gated dev endpoints set the state and
emit signed synthetic webhooks at our own receiver.

Selection mirrors `flash._choose_subscription`: by id only that id will do; by
ref, prefer one that still entitles, then the one running longest.
"""

from datetime import datetime

from app.core.flash import FlashSubscription

_subscriptions: dict[str, FlashSubscription] = {}


def set_subscription(subscription: FlashSubscription) -> None:
    _subscriptions[subscription.id] = subscription


def remove_subscription(subscription_id: str) -> bool:
    return _subscriptions.pop(subscription_id, None) is not None


def clear() -> None:
    _subscriptions.clear()


def lookup(
    subscription_id: str | None, ref: str | None
) -> FlashSubscription | None:
    matches = _matches(subscription_id, ref)
    if not matches:
        return None
    live = [row for row in matches if row.status in ("active", "trial")] or matches
    return max(live, key=lambda row: row.current_period_end or datetime.min)


def lookup_raw(subscription_id: str | None, ref: str | None) -> list[dict]:
    """Every match, in Flash's own field names — the shape `fetch_subscription_raw`
    hands to an operator, so the local fake exercises the multi-row case too."""
    return [_as_flash_row(row) for row in _matches(subscription_id, ref)]


def _matches(
    subscription_id: str | None, ref: str | None
) -> list[FlashSubscription]:
    if subscription_id:
        found = _subscriptions.get(subscription_id)
        return [found] if found else []
    return [row for row in _subscriptions.values() if row.ref == ref]


def _iso(value: datetime | None) -> str | None:
    return value.isoformat(timespec="milliseconds") + "Z" if value else None


def _as_flash_row(row: FlashSubscription) -> dict:
    return {
        "id": row.id,
        "status": row.status,
        "ref": row.ref,
        "subscriberId": row.subscriber_id,
        "serviceId": row.service_id,
        "planId": row.plan_id,
        "currentPeriodStart": _iso(row.current_period_start),
        "currentPeriodEnd": _iso(row.current_period_end),
        "nextBillingDate": _iso(row.next_billing_date),
        "trialEndDate": _iso(row.trial_end_date),
        "cancelEffectiveDate": _iso(row.cancel_effective_date),
    }
