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
            abandoned_checkouts=[],
            retired_plan_subscribers=[],
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


def test_subscribers_on_a_retired_plan_are_visible_and_reachable_in_flash(
    billing_client, monkeypatch
):
    """Withdrawing a plan from sale leaves people on it. Nothing else on this
    surface would say they exist, and ending it is Flash's to do — so the row
    carries the subscription id the admin view links out on."""
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
                abandoned_checkouts=[],
                retired_plan_subscribers=[
                    SimpleNamespace(
                        _mapping={"pubkey": PUBKEY, "flash_subscription_id": "7d3b"}
                    )
                ],
            )
        ),
    )

    body = billing_client.get("/admin/billing/divergence").json()

    section = body["retired_plan_subscribers"]
    assert section["count"] == 1
    assert section["rows"][0]["flash_subscription_id"] == "7d3b"


def test_nothing_here_offers_to_cancel_a_subscription():
    """There is no Flash cancel API. A cancel of ours would revoke the tier
    while Flash kept charging — worse than doing nothing — so the affordance is
    a link into Flash, never a route."""
    from app.routers.admin.billing.router import router

    assert not [r for r in router.routes if "cancel" in r.path]


# ---------------------------------------------------------------------------
# Editing a plan mapping — the only repair mechanism there is
# ---------------------------------------------------------------------------
def _plan_row(**overrides):
    return SimpleNamespace(
        **{
            "id": 1,
            "flash_service_id": "9c1e",
            "flash_plan_id": "4f2a",
            "scheduling_id": 7,
            "amount_minor": 200,
            "currency": "USD",
            "billing_period_unit": "month",
            "billing_period_count": 1,
            "sort_order": 0,
            "blurb": None,
            "includes": None,
            "excludes": None,
            "is_active": True,
            "created_at": NOW,
            "updated_at": NOW,
            **overrides,
        }
    )


def test_a_plan_mapping_carries_every_transcribed_value(billing_client, monkeypatch):
    """Flash has no plans endpoint, so nothing can verify price, currency or
    period — which is exactly why all of them have to be visible and editable."""
    monkeypatch.setattr(
        "app.routers.admin.billing.router.list_billing_plans_admin",
        AsyncMock(return_value=[_plan_row(sort_order=2, blurb="Best value")]),
    )

    row = billing_client.get("/admin/billing/plans").json()[0]

    assert row["billing_period_unit"] == "month"
    assert row["billing_period_count"] == 1
    assert row["sort_order"] == 2
    assert row["blurb"] == "Best value"
    assert "subscription_tier" not in row


def test_a_patch_writes_only_the_fields_it_was_sent(billing_client, monkeypatch):
    """A PATCH writes every field it includes, and an untouched form is how a
    staging policy ended up named "string" with a zero cadence."""
    update = AsyncMock(return_value=_plan_row(sort_order=5))
    monkeypatch.setattr(
        "app.routers.admin.billing.router.update_billing_plan", update
    )

    billing_client.patch("/admin/billing/plans/1", json={"sort_order": 5})

    assert update.await_args.args[2] == {"sort_order": 5}


def test_a_period_can_be_cleared_back_to_null(billing_client, monkeypatch):
    """`exclude_none` would drop this silently, leaving a wrong period on a row
    an admin believes they just corrected."""
    update = AsyncMock(return_value=_plan_row(billing_period_unit=None))
    monkeypatch.setattr(
        "app.routers.admin.billing.router.update_billing_plan", update
    )

    billing_client.patch(
        "/admin/billing/plans/1",
        json={"billing_period_unit": None, "billing_period_count": None},
    )

    assert update.await_args.args[2] == {
        "billing_period_unit": None,
        "billing_period_count": None,
    }


def test_a_billing_period_count_without_a_unit_is_refused(billing_client):
    """Unit and count are formatted as a pair; a count alone renders as nothing
    and would read as "every 2"."""
    response = billing_client.post(
        "/admin/billing/plans",
        json={
            "flash_service_id": "9c1e",
            "flash_plan_id": "4f2a",
            "scheduling_id": 7,
            "amount_minor": 200,
            "currency": "USD",
            "billing_period_count": 2,
        },
    )

    assert response.status_code == 422


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
                abandoned_checkouts=[],
                retired_plan_subscribers=[],
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


# ---------------------------------------------------------------------------
# Flash's own record, at the source
# ---------------------------------------------------------------------------
class _FakeRedis:
    """Enough of redis for the fixed-window limiter."""

    def __init__(self) -> None:
        self.counts: dict[str, int] = {}

    async def incr(self, key: str) -> int:
        self.counts[key] = self.counts.get(key, 0) + 1
        return self.counts[key]

    async def expire(self, key: str, seconds: int) -> bool:
        return True


@pytest.fixture
def flash_record_client(billing_client, monkeypatch):
    """The real limiter, over a fake redis — so the wiring is under test, not stubbed."""
    fake = _FakeRedis()
    monkeypatch.setattr(
        "app.utils.rate_limiting.rate_limiting.get_redis_client", lambda: fake
    )
    return billing_client


RAW_BODY = {
    "livemode": True,
    "subscriptions": [
        {"id": "old", "status": "expired", "ref": PUBKEY},
        {"id": "7d3b", "status": "active", "ref": PUBKEY},
    ],
}


def _raw_returns(monkeypatch, value=None, raises=None):
    mock = AsyncMock(return_value=value, side_effect=raises)
    monkeypatch.setattr(
        "app.routers.admin.billing.router.fetch_subscription_raw", mock
    )
    return mock


def test_a_subscriber_is_looked_up_by_our_own_reference(
    flash_record_client, monkeypatch
):
    raw = _raw_returns(monkeypatch, RAW_BODY)

    response = flash_record_client.get(f"/admin/billing/subscriptions/{PUBKEY}/flash")

    assert response.status_code == 200
    assert raw.await_args.kwargs == {"subscription_id": None, "ref": PUBKEY}


def test_an_unresolved_signup_is_looked_up_by_the_only_handle_it_has(
    flash_record_client, monkeypatch
):
    """It has no pubkey, so its Flash id is the sole way to inspect it."""
    raw = _raw_returns(monkeypatch, RAW_BODY)

    response = flash_record_client.get("/admin/billing/unresolved/7d3b/flash")

    assert response.status_code == 200
    assert raw.await_args.kwargs == {"subscription_id": "7d3b", "ref": None}


def test_flashs_body_arrives_unmodified_including_the_rows_we_would_discard(
    flash_record_client, monkeypatch
):
    """The disambiguation our normal lookup performs is the thing being checked."""
    _raw_returns(monkeypatch, RAW_BODY)

    body = flash_record_client.get(
        f"/admin/billing/subscriptions/{PUBKEY}/flash"
    ).json()

    assert body == RAW_BODY


def test_reading_flashs_record_applies_nothing(flash_record_client, monkeypatch):
    _raw_returns(monkeypatch, RAW_BODY)
    applied = AsyncMock()
    monkeypatch.setattr("app.routers.admin.billing.router.apply_entitlement", applied)

    flash_record_client.get(f"/admin/billing/subscriptions/{PUBKEY}/flash")

    applied.assert_not_awaited()


def test_no_such_subscription_and_could_not_ask_are_told_apart(
    flash_record_client, monkeypatch
):
    """Acting on the wrong one dismisses a real customer."""
    from app.core.flash import FlashUnavailable

    _raw_returns(monkeypatch, None)
    absent = flash_record_client.get(f"/admin/billing/subscriptions/{PUBKEY}/flash")

    _raw_returns(monkeypatch, raises=FlashUnavailable("socket timed out"))
    unreachable = flash_record_client.get(
        f"/admin/billing/subscriptions/{PUBKEY}/flash"
    )

    assert absent.status_code == 404
    assert unreachable.status_code == 503
    # The frontend renders `detail` as a string, never a dict.
    assert isinstance(absent.json()["detail"], str)
    assert isinstance(unreachable.json()["detail"], str)


def test_a_refused_credential_is_reported_rather_than_retried(
    flash_record_client, monkeypatch
):
    from app.core.config import settings
    from app.core.flash import FlashCredentialError

    raw = _raw_returns(
        monkeypatch, raises=FlashCredentialError("Flash refused our credentials (401)")
    )

    response = flash_record_client.get(
        f"/admin/billing/subscriptions/{PUBKEY}/flash"
    )

    assert response.status_code == 502
    assert response.status_code != 503  # not mistaken for a passing outage
    assert raw.await_count == 1
    assert settings.flash_api_key not in response.text


def test_the_control_cannot_be_turned_into_a_quota_incident(
    flash_record_client, monkeypatch
):
    from app.utils.rate_limiting import rate_limiting

    raw = _raw_returns(monkeypatch, RAW_BODY)
    url = f"/admin/billing/subscriptions/{PUBKEY}/flash"

    for _ in range(rate_limiting.FLASH_RECORD_RATE_LIMIT):
        assert flash_record_client.get(url).status_code == 200

    assert flash_record_client.get(url).status_code == 429
    assert raw.await_count == rate_limiting.FLASH_RECORD_RATE_LIMIT


def test_flashs_record_is_not_readable_without_billing_access(client, monkeypatch):
    monkeypatch.setattr(settings, "billing_admin_whitelisted_pubkeys", OTHER)
    wl.init_billing_admin_whitelist()

    assert (
        client.get(f"/admin/billing/subscriptions/{PUBKEY}/flash").status_code == 403
    )
    assert client.get("/admin/billing/unresolved/7d3b/flash").status_code == 403
