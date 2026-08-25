"""What an operator can see, and who counts as an operator.

Two questions the surface exists to keep apart: what Flash says we are charging
someone, and what the scheduler actually gives them. Where those disagree is the
bug — someone paying who isn't being recalculated, or someone on the paid cadence
who stopped paying — and it should be findable by sorting a column.
"""

from datetime import datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.core import billing_admin_whitelist as wl
from app.core.config import settings
from app.core.database import get_db

PUBKEY = "a" * 64
OTHER = "b" * 64
NOW = datetime(2026, 8, 25, 12, 0, 0)


@pytest.fixture(autouse=True)
def reset_whitelist():
    wl._billing_pubkeys = set()
    yield
    wl._billing_pubkeys = set()


# ---------------------------------------------------------------------------
# Who counts as an operator
# ---------------------------------------------------------------------------
def test_billing_access_uses_its_own_list_when_one_is_configured(monkeypatch):
    monkeypatch.setattr(settings, "billing_admin_whitelisted_pubkeys", PUBKEY)
    monkeypatch.setattr(settings, "admin_whitelisted_pubkeys", OTHER)

    wl.init_billing_admin_whitelist()

    assert wl.get_billing_pubkeys() == {PUBKEY}


def test_billing_access_falls_back_to_the_administrators_when_unset(monkeypatch):
    """So an existing deployment keeps working without a new variable."""
    monkeypatch.setattr(settings, "billing_admin_whitelisted_pubkeys", "")
    monkeypatch.setattr(settings, "admin_whitelisted_pubkeys", OTHER)

    wl.init_billing_admin_whitelist()

    assert wl.get_billing_pubkeys() == {OTHER}


def test_a_configured_billing_list_does_not_also_admit_the_administrators(monkeypatch):
    """The point of the separate list is that it is separate."""
    monkeypatch.setattr(settings, "billing_admin_whitelisted_pubkeys", PUBKEY)
    monkeypatch.setattr(settings, "admin_whitelisted_pubkeys", OTHER)

    wl.init_billing_admin_whitelist()

    assert OTHER not in wl.get_billing_pubkeys()


def test_several_people_can_be_authorised(monkeypatch):
    monkeypatch.setattr(
        settings, "billing_admin_whitelisted_pubkeys", f"{PUBKEY}, {OTHER}"
    )

    wl.init_billing_admin_whitelist()

    assert wl.get_billing_pubkeys() == {PUBKEY, OTHER}


# ---------------------------------------------------------------------------
# The gate
# ---------------------------------------------------------------------------
@pytest.fixture
def billing_client(client, caller, monkeypatch):
    from app.api import app

    async def _fake_get_db():
        yield AsyncMock()

    monkeypatch.setattr(settings, "billing_admin_whitelisted_pubkeys", caller.pubkey)
    wl.init_billing_admin_whitelist()
    app.dependency_overrides[get_db] = _fake_get_db
    yield client


def test_someone_not_on_the_list_is_refused(client, caller, monkeypatch):
    monkeypatch.setattr(settings, "billing_admin_whitelisted_pubkeys", OTHER)
    wl.init_billing_admin_whitelist()

    assert client.get("/admin/billing/subscriptions").status_code == 403


def test_billing_access_does_not_confer_general_administration(
    billing_client, caller, monkeypatch
):
    monkeypatch.setattr(settings, "admin_enabled", True)
    """Whoever answers billing questions should not thereby be able to rotate
    signing keys."""
    monkeypatch.setattr(settings, "admin_whitelisted_pubkeys", OTHER)
    from app.core.admin_whitelist import init_admin_whitelist

    init_admin_whitelist()

    assert billing_client.get("/admin/stats").status_code == 403


# ---------------------------------------------------------------------------
# The two questions, kept apart
# ---------------------------------------------------------------------------
def _row(
    pubkey=PUBKEY,
    flash_status="active",
    granted=7,
    actual=7,
    source="billing",
    synced=NOW,
    error=None,
):
    return SimpleNamespace(
        pubkey=pubkey,
        flash_status=flash_status,
        granted_scheduling_id=granted,
        granted_scheduling_name="Priority" if granted else None,
        scheduling_id=actual,
        scheduling_name="Priority" if actual else "Weekly",
        scheduling_source=source,
        current_period_end=NOW + timedelta(days=20),
        last_synced_at=synced,
        last_sync_error=error,
        billing_blocked=False,
    )


def test_a_row_shows_what_flash_says_beside_what_they_actually_get():
    """The two are separate fields on purpose: one is what we are charging for,
    the other is what the scheduler will actually do. Collapsing them would hide
    exactly the disagreement this surface exists to find."""
    from app.schemas.schemas import BillingSubscriptionItem

    item = BillingSubscriptionItem.model_validate(
        _row(flash_status="active", granted=7, actual=None), from_attributes=True
    )

    assert item.flash_status == "active"
    assert item.granted_scheduling_id == 7
    assert item.scheduling_id is None


def test_a_row_says_who_put_them_on_their_policy():
    """Admin-granted, billing-granted and a bug must be tellable apart."""
    from app.schemas.schemas import BillingSubscriptionItem

    comped = BillingSubscriptionItem.model_validate(
        _row(source="admin"), from_attributes=True
    )
    paid = BillingSubscriptionItem.model_validate(
        _row(source="billing"), from_attributes=True
    )

    assert comped.scheduling_source == "admin"
    assert paid.scheduling_source == "billing"


def test_a_divergence_report_names_each_disagreement(billing_client, monkeypatch):
    report = AsyncMock(
        return_value=SimpleNamespace(
            policy_mismatch=[SimpleNamespace(_mapping={"pubkey": PUBKEY})],
            admin_overrides=[],
            stale_syncs=[],
            failing_syncs=[],
            unresolved_events=[],
            unrecognised_statuses=[],
            exhausted_events=[],
        )
    )
    monkeypatch.setattr(
        "app.services.billing_visibility_service.build_divergence_report", report
    )

    response = billing_client.get("/admin/billing/divergence")

    assert response.status_code == 200
    body = response.json()
    assert body["policy_mismatch"]["count"] == 1
    assert body["stale_syncs"]["count"] == 0


def test_an_operator_can_force_one_subscriber_to_resynchronise(
    billing_client, monkeypatch
):
    applied = AsyncMock(
        return_value=SimpleNamespace(applied=True, reason=SimpleNamespace(value="granted"))
    )
    monkeypatch.setattr("app.routers.admin.billing.router.apply_entitlement", applied)

    response = billing_client.post(f"/admin/billing/subscriptions/{PUBKEY}/resync")

    assert response.status_code == 200
    assert applied.await_args.kwargs["external_ref"] == PUBKEY


def test_payment_history_can_be_exported(billing_client, monkeypatch):
    monkeypatch.setattr(
        "app.services.billing_visibility_service.select_payment_history_on_db",
        AsyncMock(
            return_value=[
                SimpleNamespace(
                    paid_at="2026-08-20T14:03:11.000Z",
                    pubkey=PUBKEY,
                    subscription_id="7d3b",
                    invoice_id="inv_1",
                    amount_minor=200,
                    currency="USD",
                )
            ]
        ),
    )

    response = billing_client.get("/admin/billing/export.csv")

    assert response.status_code == 200
    assert "text/csv" in response.headers["content-type"]
    assert "inv_1" in response.text
    assert "amount_minor" in response.text


def test_the_export_is_not_reachable_without_billing_access(client, monkeypatch):
    monkeypatch.setattr(settings, "billing_admin_whitelisted_pubkeys", OTHER)
    wl.init_billing_admin_whitelist()

    assert client.get("/admin/billing/export.csv").status_code == 403


def test_billing_visibility_survives_general_administration_being_off(
    billing_client, monkeypatch
):
    """Turning off admin routes should not blind whoever handles payments —
    whether this surface exists at all is decided by flash_enabled."""
    monkeypatch.setattr(settings, "admin_enabled", False)
    monkeypatch.setattr(
        "app.services.billing_visibility_service.build_divergence_report",
        AsyncMock(
            return_value=SimpleNamespace(
                policy_mismatch=[],
                admin_overrides=[],
                stale_syncs=[],
                failing_syncs=[],
                unresolved_events=[],
                unrecognised_statuses=[],
                exhausted_events=[],
            )
        ),
    )

    assert billing_client.get("/admin/billing/divergence").status_code == 200


def test_the_billing_surface_is_absent_where_payments_are_not_configured(monkeypatch):
    from fastapi import APIRouter
    from app.routers.router import include_billing_routers

    monkeypatch.setattr(settings, "flash_enabled", False)
    bare = APIRouter()

    include_billing_routers(bare)

    assert bare.routes == []
