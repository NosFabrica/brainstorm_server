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
    if subscription_id:
        return _subscriptions.get(subscription_id)
    matches = [row for row in _subscriptions.values() if row.ref == ref]
    if not matches:
        return None
    live = [row for row in matches if row.status in ("active", "trial")] or matches
    return max(live, key=lambda row: row.current_period_end or datetime.min)
