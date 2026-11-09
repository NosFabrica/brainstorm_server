"""In-process Flash, mounted as the real client's transport for local runs.

Proves our pipeline against what we believe Flash sends, not that the belief is
right. Imports nothing from `flash.py`, which imports this.
"""

import json
from collections.abc import Callable
from copy import deepcopy
from urllib.parse import unquote

import httpx

_API = "/api/v1/external/"
_NOT_CANCELLABLE = ("canceled", "expired")
# The status a PATCH may ask for, and the only status it may leave.
_SETTABLE_FROM = {"paused": "active", "active": "paused"}

_ROW_DEFAULTS: dict = {
    "status": "active",
    "ref": None,
    "subscriberId": None,
    "serviceId": "",
    "planId": "",
    "pricingSnapshot": None,
    "dunningPolicy": {
        "maxAttempts": 3,
        "retryIntervalDays": 3,
        "gracePeriodDays": 7,
        "cancelAfterFinalFailure": True,
    },
    "cancellationPolicy": {
        "mode": "end_of_period",
        "minimumCommitmentPeriods": 0,
        "noticePeriodDays": 0,
    },
    "currentPeriodStart": None,
    "currentPeriodEnd": None,
    "currentPeriodNumber": 1,
    "nextBillingDate": None,
    "anchorDate": None,
    "trialEndDate": None,
    "cancelEffectiveDate": None,
    "dunningAttempts": 0,
    "portalUrl": None,
}

_subscriptions: dict[str, dict] = {}
_plans: dict[tuple[str, str], dict] = {}


class UnroutedFlashRequest(Exception):
    """Raised, not answered: a 404 means "absent" to the client."""


def set_subscription(fields: dict) -> None:
    _subscriptions[fields["id"]] = {**deepcopy(_ROW_DEFAULTS), **fields}


def remove_subscription(subscription_id: str) -> bool:
    return _subscriptions.pop(subscription_id, None) is not None


def set_plan(plan: dict) -> None:
    _plans[(str(plan.get("serviceId") or ""), str(plan.get("id") or ""))] = plan


def remove_plan(service_id: str, plan_id: str) -> bool:
    return _plans.pop((service_id, plan_id), None) is not None


def clear() -> None:
    _subscriptions.clear()
    _plans.clear()


def transport() -> httpx.MockTransport:
    return httpx.MockTransport(_handle)


def _handle(request: httpx.Request) -> httpx.Response:
    path = request.url.raw_path.decode().partition("?")[0]
    route = (request.method, *map(unquote, path.removeprefix(_API).split("/")))
    if path.startswith(_API):
        match route:
            case ("GET", "subscriptions"):
                return _list_by_ref(request.url.params.get("ref"))
            case ("GET", "subscriptions", subscription_id):
                return _on_subscription(subscription_id, _subscription_body)
            case ("POST", "subscriptions", subscription_id, "cancel"):
                return _on_subscription(subscription_id, _cancel)
            case ("PATCH", "subscriptions", subscription_id):
                body = json.loads(request.content)
                return _on_subscription(
                    subscription_id, lambda found: _set_status(found, body)
                )
            case ("GET", "services"):
                return _ok(services=_services())
            case ("GET", "services", service_id):
                return _read_service(service_id)
    raise UnroutedFlashRequest(f"{request.method} {path}")


def _ok(**body: object) -> httpx.Response:
    return httpx.Response(200, json={"livemode": True, **body})


def _error(status: int, message: str) -> httpx.Response:
    return httpx.Response(status, json={"message": message})


def _list_by_ref(ref: str | None) -> httpx.Response:
    return _ok(subscriptions=[r for r in _subscriptions.values() if r["ref"] == ref])


def _on_subscription(
    subscription_id: str, act: Callable[[dict], httpx.Response]
) -> httpx.Response:
    found = _subscriptions.get(subscription_id)
    if found is None:
        return _error(404, "Subscription not found")
    return act(found)


def _subscription_body(found: dict) -> httpx.Response:
    return _ok(subscription=found)


def _services() -> list[dict]:
    """The services the seeded plans imply: one per distinct `serviceId`."""
    return [
        {"id": service, "name": service, "description": None, "signupUrl": None}
        for service in dict.fromkeys(service for service, _ in _plans)
    ]


def _read_service(service_id: str) -> httpx.Response:
    service = next((s for s in _services() if s["id"] == service_id), None)
    if service is None:
        return _error(404, "Service not found")
    plans = [plan for (owner, _), plan in _plans.items() if owner == service_id]
    return _ok(service=service, plans=plans)


def _cancel(found: dict) -> httpx.Response:
    if found["status"] in _NOT_CANCELLABLE:
        return _error(400, "Subscription has already ended")
    policy = found["cancellationPolicy"]
    if isinstance(policy, dict) and policy.get("mode") == "immediate":
        found.update(status="canceled", nextBillingDate=None)
    else:
        found["cancelEffectiveDate"] = found["currentPeriodEnd"]
    return _ok(subscription=found)


def _set_status(found: dict, body: dict) -> httpx.Response:
    requested = body.get("status", "")
    if found["status"] != _SETTABLE_FROM.get(requested):
        return _error(400, f"Cannot move {found['status']} to {requested}")
    found["status"] = requested
    return _ok(subscription=found)
