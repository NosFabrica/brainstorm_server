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
from pydantic import BaseModel

from app.core import flash_mock
from app.core.config import settings
from app.core.flash import FlashSubscription
from app.services.flash_webhook_service import compute_signature

router = APIRouter()


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


@router.put(path="/subscription", summary="Dev: set one mock Flash subscription")
async def set_mock_subscription_endpoint(body: MockSubscriptionBody):
    flash_mock.set_subscription(FlashSubscription(**body.model_dump()))
    return {"ok": True, "mock_enabled": settings.flash_mock_enabled}


@router.delete(
    path="/subscription/{subscription_id}",
    summary="Dev: remove one mock Flash subscription",
)
async def remove_mock_subscription_endpoint(subscription_id: str):
    return {"removed": flash_mock.remove_subscription(subscription_id)}


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
