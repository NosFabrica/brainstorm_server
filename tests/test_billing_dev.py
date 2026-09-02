"""The LOCAL-only billing test surface: the mock Flash store and the webhook
emitter. These are mounted only on LOCAL deployments, so this suite drives the
router directly rather than through the aggregated app.

The emitter's promise is exercised end-to-end: what it signs must be accepted
by the real receiver, or the local drill proves nothing about production.
"""

from datetime import datetime
from unittest.mock import AsyncMock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.core import flash_mock
from app.core.config import settings
from app.core.flash import FlashSubscription, fetch_subscription
from app.routers.admin.billing.dev import router as dev_router

NOW = datetime(2026, 8, 25, 12, 0, 0)
PUBKEY = "a" * 64


@pytest.fixture(autouse=True)
def clean_mock_store():
    flash_mock.clear()
    yield
    flash_mock.clear()


@pytest.fixture
def dev_client():
    app = FastAPI()
    app.include_router(dev_router, prefix="/admin/billing/dev")
    return TestClient(app)


def _subscription(**overrides) -> dict:
    body = {
        "id": "7d3b",
        "status": "active",
        "ref": PUBKEY,
        "service_id": "9c1e",
        "plan_id": "4f2a",
        "current_period_end": "2026-09-25T12:00:00",
    }
    body.update(overrides)
    return body


def test_the_store_answers_by_id_and_by_ref(dev_client, monkeypatch):
    monkeypatch.setattr(settings, "flash_mock_enabled", True)
    dev_client.put("/admin/billing/dev/subscription", json=_subscription())

    import asyncio

    by_id = asyncio.run(fetch_subscription(subscription_id="7d3b"))
    by_ref = asyncio.run(fetch_subscription(ref=PUBKEY))

    assert by_id is not None and by_id.status == "active"
    assert by_ref is not None and by_ref.id == "7d3b"


def test_by_ref_prefers_the_subscription_that_still_entitles(dev_client):
    """Same rule as the real client: a re-subscribe leaves several rows under
    one ref, and picking the expired one would revoke someone paying."""
    dev_client.put(
        "/admin/billing/dev/subscription",
        json=_subscription(id="old", status="expired"),
    )
    dev_client.put(
        "/admin/billing/dev/subscription",
        json=_subscription(id="new", status="active"),
    )

    chosen = flash_mock.lookup(None, PUBKEY)

    assert chosen is not None and chosen.id == "new"


def test_a_removed_subscription_is_a_fact_not_an_error(dev_client):
    dev_client.put("/admin/billing/dev/subscription", json=_subscription())
    assert dev_client.delete("/admin/billing/dev/subscription/7d3b").json() == {
        "removed": True
    }
    assert flash_mock.lookup("7d3b", None) is None


def test_the_emitter_signs_what_the_real_receiver_accepts(
    dev_client, monkeypatch
):
    """The whole point of the emitter: the genuine verify → record → ack path,
    not a shortcut around it."""
    from app.api import app as real_app
    from app.core.database import get_db
    from app.services import flash_webhook_service as svc

    monkeypatch.setattr(settings, "flash_webhook_secret", "whsec_dev_secret")

    async def _fake_get_db():
        yield AsyncMock()

    real_app.dependency_overrides[get_db] = _fake_get_db
    monkeypatch.setattr(
        svc, "insert_flash_webhook_event_on_db", AsyncMock(return_value=1)
    )
    monkeypatch.setattr(svc, "apply_entitlement", AsyncMock())
    monkeypatch.setattr(svc, "mark_webhook_event_processed_on_db", AsyncMock())
    try:
        response = dev_client.post(
            "/admin/billing/dev/emit-webhook",
            json={
                "event": "subscription.activated",
                "data": {"subscriptionId": "7d3b", "externalRef": PUBKEY},
            },
        )
    finally:
        real_app.dependency_overrides.pop(get_db, None)

    assert response.status_code == 200
    assert response.json()["delivered_status"] == 200
    assert response.json()["delivered_body"] == {"ok": True, "duplicate": False}


def test_the_emitter_refuses_to_sign_with_nothing(dev_client, monkeypatch):
    monkeypatch.setattr(settings, "flash_webhook_secret", "")
    response = dev_client.post(
        "/admin/billing/dev/emit-webhook",
        json={"event": "subscription.activated", "data": {}},
    )
    assert response.status_code == 409
