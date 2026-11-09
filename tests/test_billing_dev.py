"""The LOCAL-only billing test surface: seeding the fake Flash and the webhook
emitter. These are mounted only on LOCAL deployments, so this suite drives the
router directly rather than through the aggregated app.

The emitter's promise is exercised end-to-end: what it signs must be accepted
by the real receiver, or the local drill proves nothing about production.
"""

import asyncio
from datetime import datetime
from unittest.mock import AsyncMock

import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.core import flash
from app.core.config import settings
from app.core.flash import (
    FlashRefused,
    FlashServiceMissing,
    FlashUnavailable,
    cancel_subscription,
    fetch_service_plans_raw,
    fetch_services,
    fetch_subscription,
    set_subscription_status,
)
from app.routers.admin.billing.dev import router as dev_router

PUBKEY = "a" * 64

pytestmark = pytest.mark.usefixtures("fake_flash")


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
        "serviceId": "9c1e",
        "planId": "4f2a",
        "currentPeriodEnd": "2026-09-25T12:00:00.000Z",
    }
    body.update(overrides)
    return body


def test_the_store_answers_by_id_and_by_ref(dev_client):
    dev_client.put("/admin/billing/dev/subscription", json=_subscription())

    by_id = asyncio.run(fetch_subscription(subscription_id="7d3b"))
    by_ref = asyncio.run(fetch_subscription(ref=PUBKEY))

    assert by_id is not None and by_id.status == "active"
    assert by_ref is not None and by_ref.id == "7d3b"


def test_a_removed_subscription_is_a_fact_not_an_error(dev_client):
    dev_client.put("/admin/billing/dev/subscription", json=_subscription())
    assert dev_client.delete("/admin/billing/dev/subscription/7d3b").json() == {
        "removed": True
    }
    assert asyncio.run(fetch_subscription(subscription_id="7d3b")) is None


def test_the_local_cancel_ends_the_period_rather_than_the_subscription(dev_client):
    """The local rehearsal has to reproduce the trap, or it rehearses nothing:
    the account cancels at period end, so the fake must answer with the status
    untouched and the effective date set."""
    dev_client.put("/admin/billing/dev/subscription", json=_subscription())

    cancelled = asyncio.run(cancel_subscription("7d3b"))

    assert cancelled is not None
    assert cancelled.status == "active"
    assert cancelled.cancel_effective_date == datetime(2026, 9, 25, 12, 0, 0)


def test_the_local_pause_and_resume_are_visible_to_the_next_read(dev_client):
    dev_client.put("/admin/billing/dev/subscription", json=_subscription())

    asyncio.run(set_subscription_status("7d3b", status="paused"))
    paused = asyncio.run(fetch_subscription(subscription_id="7d3b"))
    asyncio.run(set_subscription_status("7d3b", status="active"))
    resumed = asyncio.run(fetch_subscription(subscription_id="7d3b"))

    assert paused is not None and paused.status == "paused"
    assert resumed is not None and resumed.status == "active"


def test_writing_to_something_the_local_store_does_not_hold_is_absence():
    assert asyncio.run(cancel_subscription("nosuch")) is None
    assert asyncio.run(set_subscription_status("nosuch", status="paused")) is None


def test_an_immediate_cancel_ends_the_subscription_now(dev_client):
    dev_client.put(
        "/admin/billing/dev/subscription",
        json=_subscription(
            cancellationPolicy={"mode": "immediate"},
            nextBillingDate="2026-09-25T12:00:00.000Z",
        ),
    )

    cancelled = asyncio.run(cancel_subscription("7d3b"))

    assert cancelled is not None
    assert cancelled.status == "canceled"
    assert cancelled.next_billing_date is None


@pytest.mark.parametrize("ended", ["canceled", "expired"])
def test_cancelling_a_subscription_already_over_is_refused(dev_client, ended):
    dev_client.put("/admin/billing/dev/subscription", json=_subscription(status=ended))

    with pytest.raises(FlashRefused):
        asyncio.run(cancel_subscription("7d3b"))


@pytest.mark.parametrize(
    ("current", "requested"),
    [("expired", "active"), ("active", "active"), ("paused", "paused")],
)
def test_a_status_change_flash_has_no_transition_for_is_refused(
    dev_client, current, requested
):
    dev_client.put(
        "/admin/billing/dev/subscription", json=_subscription(status=current)
    )

    with pytest.raises(FlashRefused):
        asyncio.run(set_subscription_status("7d3b", status=requested))


def test_a_seeded_bare_date_runs_to_the_end_of_its_day(dev_client):
    dev_client.put(
        "/admin/billing/dev/subscription",
        json=_subscription(currentPeriodEnd="2026-09-25"),
    )

    found = asyncio.run(fetch_subscription(subscription_id="7d3b"))

    assert found is not None
    assert found.current_period_end == datetime(2026, 9, 25, 23, 59, 59, 999999)


def test_seeded_plans_are_what_the_service_reads_answer(dev_client):
    dev_client.put(
        "/admin/billing/dev/plan",
        json={"id": "4f2a", "serviceId": "9c1e", "name": "Local", "amount": "1"},
    )

    services = asyncio.run(fetch_services())
    body = asyncio.run(fetch_service_plans_raw("9c1e"))

    assert [service.id for service in services] == ["9c1e"]
    assert [plan["name"] for plan in body["plans"]] == ["Local"]


def test_a_service_with_no_seeded_plans_does_not_exist(dev_client):
    dev_client.put(
        "/admin/billing/dev/plan",
        json={"id": "4f2a", "serviceId": "9c1e", "name": "Local", "amount": "1"},
    )
    dev_client.delete("/admin/billing/dev/plan/9c1e/4f2a")

    with pytest.raises(FlashServiceMissing):
        asyncio.run(fetch_service_plans_raw("9c1e"))


def test_the_setting_mounts_the_fake_under_the_real_client(dev_client, monkeypatch):
    flash._client = None
    monkeypatch.setattr(settings, "flash_mock_enabled", True)
    dev_client.put("/admin/billing/dev/subscription", json=_subscription())

    found = asyncio.run(fetch_subscription(subscription_id="7d3b"))

    assert found is not None and found.id == "7d3b"


def test_without_the_setting_the_client_keeps_the_real_transport(monkeypatch):
    flash._client = None
    monkeypatch.setattr(settings, "flash_mock_enabled", False)

    assert not isinstance(flash._get_client()._transport, httpx.MockTransport)


def test_a_request_the_fake_has_no_route_for_is_never_read_as_absence():
    with pytest.raises(FlashUnavailable, match="UnroutedFlashRequest"):
        asyncio.run(
            flash._read_body(
                "https://flash.example/api/v1/external/invoices",
                {},
                absent_on_404=True,
            )
        )


def test_an_unknown_id_with_a_slash_in_it_is_absence():
    assert asyncio.run(fetch_subscription(subscription_id="a/b")) is None


def test_a_seeded_row_reports_no_failed_charges_by_default(dev_client):
    dev_client.put("/admin/billing/dev/subscription", json=_subscription())

    found = asyncio.run(fetch_subscription(subscription_id="7d3b"))

    assert found is not None and found.policy.dunning_attempts == 0


def test_an_unreadable_seeded_policy_cancels_at_period_end(dev_client):
    dev_client.put(
        "/admin/billing/dev/subscription",
        json=_subscription(cancellationPolicy="immediate"),
    )

    cancelled = asyncio.run(cancel_subscription("7d3b"))

    assert cancelled is not None and cancelled.status == "active"


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
