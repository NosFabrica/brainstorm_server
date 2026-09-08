"""LOCAL-only billing test surface: mock Flash state, and a webhook emitter
that signs a synthetic payload with the real secret and POSTs it at our own
receiver — exercising the genuine verify → dedupe → entitlement path.

Mounted only when `deploy_environment == LOCAL` (see `include_billing_routers`).
"""

import json
import time
from datetime import datetime, timezone

import httpx
from fastapi import APIRouter, HTTPException, status
from pydantic import BaseModel, ConfigDict, Field

from app.core import flash_mock
from app.core.config import settings
from app.core.flash import FlashPricing, FlashSubscription
from app.services.flash_webhook_service import compute_signature

router = APIRouter()


class MockPricingBody(BaseModel):
    """The price Flash snapshotted at signup — what the subscriber's card is
    priced from. Omit it to drill a subscription Flash priced nowhere."""

    amount_minor: int | None = None
    currency: str | None = None
    billing_interval: str | None = None


class MockSubscriptionBody(BaseModel):
    id: str
    status: str
    ref: str | None = None
    subscriber_id: str | None = None
    service_id: str
    plan_id: str
    current_period_start: datetime | None = None
    current_period_end: datetime | None = None
    next_billing_date: datetime | None = None
    trial_end_date: datetime | None = None
    cancel_effective_date: datetime | None = None
    portal_url: str | None = None
    pricing: MockPricingBody | None = None


@router.put(path="/subscription", summary="Dev: set one mock Flash subscription")
async def set_mock_subscription_endpoint(body: MockSubscriptionBody):
    fields = body.model_dump()
    fields["pricing"] = (
        FlashPricing(**fields["pricing"]) if fields["pricing"] is not None else None
    )
    flash_mock.set_subscription(FlashSubscription(**fields))
    return {"ok": True, "mock_enabled": settings.flash_mock_enabled}


@router.delete(
    path="/subscription/{subscription_id}",
    summary="Dev: remove one mock Flash subscription",
)
async def remove_mock_subscription_endpoint(subscription_id: str):
    return {"removed": flash_mock.remove_subscription(subscription_id)}


class MockPlanBody(BaseModel):
    """One plan, taken and stored in Flash's own field names so the fake and the
    real read parse identically. Only what the pricing page uses."""

    model_config = ConfigDict(populate_by_name=True)

    id: str
    service_id: str = Field(alias="serviceId")
    name: str
    amount: str
    currency: str = "USD"
    billing_interval: str | None = Field(default=None, alias="billingInterval")
    description: str | None = None
    features: list[str] | None = None
    not_included: list[str] | None = Field(default=None, alias="notIncluded")
    sort_order: int = Field(default=0, alias="sortOrder")
    status: str = "active"
    # Nothing synthesizes a checkout URL any more, so a mock plan without one
    # rehearses a plan we cannot sell — and drops off the local pricing page.
    signup_url: str | None = Field(default=None, alias="signupUrl")


@router.put(path="/plan", summary="Dev: set one mock Flash plan")
async def set_mock_plan_endpoint(body: MockPlanBody):
    flash_mock.set_plan(body.model_dump(by_alias=True))
    return {"ok": True, "mock_enabled": settings.flash_mock_enabled}


@router.delete(
    path="/plan/{service_id}/{plan_id}", summary="Dev: remove one mock Flash plan"
)
async def remove_mock_plan_endpoint(service_id: str, plan_id: str):
    return {"removed": flash_mock.remove_plan(service_id, plan_id)}


class EmitWebhookBody(BaseModel):
    event: str
    data: dict


@router.post(
    path="/emit-webhook",
    summary="Dev: sign a synthetic Flash webhook and deliver it to ourselves",
)
async def emit_webhook_endpoint(body: EmitWebhookBody):
    if not settings.flash_webhook_secret:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="flash_webhook_secret is empty; nothing to sign with",
        )

    payload = {
        "event": body.event,
        "timestamp": datetime.now(timezone.utc).isoformat(timespec="milliseconds")
        .replace("+00:00", "Z"),
        "data": body.data,
    }
    raw = json.dumps(payload).encode()
    now = int(time.time())
    signature = compute_signature(settings.flash_webhook_secret, now, raw)

    # Delivered through the real route, in-process: the ASGI transport walks the
    # same verify → record → ack path a genuine delivery does.
    from app.api import app

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(
        transport=transport, base_url="http://flash-dev-emitter"
    ) as client:
        response = await client.post(
            "/webhooks/flash",
            content=raw,
            headers={
                "Flash-Signature": f"t={now},v1={signature}",
                "Content-Type": "application/json",
            },
        )

    return {
        "delivered_status": response.status_code,
        "delivered_body": response.json(),
    }
