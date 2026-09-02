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


def test_the_roster_hands_an_operator_the_day_flash_named():
    """Same shape rule as the subscriber's card: a boundary read from a bare
    date goes out as that date, so no operator's timezone can shift it. Our own
    sync time is a real instant and stays one."""
    from app.schemas.schemas import BillingSubscriptionItem

    item = BillingSubscriptionItem.model_validate(
        _row(synced=datetime(2026, 9, 19, 2, 0)), from_attributes=True
    ).model_copy(
        update={"current_period_end": datetime(2026, 9, 20, 23, 59, 59, 999999)}
    )

    wire = item.model_dump(mode="json")

    assert wire["current_period_end"] == "2026-09-20"
    assert wire["last_synced_at"] == "2026-09-19T02:00:00Z"


def test_a_divergence_report_names_each_disagreement(billing_client, monkeypatch):
    report = AsyncMock(
        return_value=SimpleNamespace(
            policy_mismatch=[SimpleNamespace(_mapping={"pubkey": PUBKEY})],
            admin_overrides=[],
            stale_syncs=[],
            failing_syncs=[],
            unresolved_signups=[],
            unmapped_plans=[],
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
                unresolved_signups=[],
                unmapped_plans=[],
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
            "is_active": True,
            "created_at": NOW,
            "updated_at": NOW,
            **overrides,
        }
    )


def test_a_plan_mapping_is_the_two_decisions_flash_cannot_make(
    billing_client, monkeypatch
):
    """Which policy buying it grants, and whether we sell it. Price, currency,
    period, ordering and copy are read from Flash and are not editable here,
    because they are no longer ours to get wrong."""
    monkeypatch.setattr(
        "app.routers.admin.billing.router.list_billing_plans_admin",
        AsyncMock(return_value=[_plan_row()]),
    )

    row = billing_client.get("/admin/billing/plans").json()[0]

    assert row["scheduling_id"] == 7
    assert row["is_active"] is True
    assert set(row) == {
        "id",
        "flash_service_id",
        "flash_plan_id",
        "scheduling_id",
        "is_active",
        "created_at",
        "updated_at",
    }


def test_a_patch_writes_only_the_fields_it_was_sent(billing_client, monkeypatch):
    """A PATCH writes every field it includes, and an untouched form is how a
    staging policy ended up named "string" with a zero cadence."""
    update = AsyncMock(return_value=_plan_row(is_active=False))
    monkeypatch.setattr(
        "app.routers.admin.billing.router.update_billing_plan", update
    )

    billing_client.patch("/admin/billing/plans/1", json={"is_active": False})

    assert update.await_args.args[2] == {"is_active": False}


def test_editing_a_price_here_is_refused_rather_than_ignored(billing_client):
    """It is Flash's now. Accepting the field and dropping it would let someone
    correct a price on this form and watch the pricing page ignore them."""
    response = billing_client.patch(
        "/admin/billing/plans/1", json={"amount_minor": 200}
    )

    assert response.status_code == 422


def test_a_mapping_is_created_from_the_two_decisions_alone(
    billing_client, monkeypatch
):
    create = AsyncMock(return_value=_plan_row())
    monkeypatch.setattr("app.routers.admin.billing.router.create_billing_plan", create)

    response = billing_client.post(
        "/admin/billing/plans",
        json={
            "flash_service_id": "9c1e",
            "flash_plan_id": "4f2a",
            "scheduling_id": 7,
        },
    )

    assert response.status_code == 201
    assert create.await_args.args[1] == {
        "flash_service_id": "9c1e",
        "flash_plan_id": "4f2a",
        "scheduling_id": 7,
        "is_active": True,
    }


def test_a_flash_id_typo_is_a_one_field_edit(billing_client, monkeypatch):
    """They used to be rejected outright, which turned a typo in a row nobody
    ever bought into a create-plus-deactivate dance."""
    update = AsyncMock(return_value=_plan_row(flash_plan_id="beef"))
    monkeypatch.setattr("app.routers.admin.billing.router.update_billing_plan", update)

    response = billing_client.patch(
        "/admin/billing/plans/1", json={"flash_plan_id": "beef"}
    )

    assert response.status_code == 200
    assert update.await_args.args[2] == {"flash_plan_id": "beef"}


def test_a_flash_id_cannot_be_nulled(billing_client):
    """Every field here defaults to None, so an explicit null has to be caught
    before `exclude_unset` writes it to a NOT NULL column."""
    response = billing_client.patch(
        "/admin/billing/plans/1", json={"flash_service_id": None}
    )

    assert response.status_code == 422


def test_a_mapping_cannot_be_created_with_values_flash_owns(billing_client):
    """The same refusal as on the edit, so a stale client cannot create a
    mapping believing it has set a price."""
    response = billing_client.post(
        "/admin/billing/plans",
        json={
            "flash_service_id": "9c1e",
            "flash_plan_id": "4f2a",
            "scheduling_id": 7,
            "amount_minor": 200,
            "currency": "USD",
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
                unresolved_signups=[],
                unmapped_plans=[],
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


# ---------------------------------------------------------------------------
# Acting on a signup that named nobody
# ---------------------------------------------------------------------------
def _resolution(subscription_id="7d3b", resolution="attributed", pubkey=None):
    from app.services.billing_service import EntitlementReason, ResolutionOutcome

    return ResolutionOutcome(
        subscription_id=subscription_id,
        resolution=EntitlementReason(resolution),
        pubkey=pubkey,
        applied=pubkey is not None,
        events_settled=1,
    )


def test_attributing_names_the_admin_who_did_it(billing_client, caller, monkeypatch):
    """Taken from the JWT, never from the body — an audit trail the caller can
    write is not one."""
    attribute = AsyncMock(return_value=_resolution(pubkey=PUBKEY))
    monkeypatch.setattr(
        "app.routers.admin.billing.router.attribute_unresolved_subscription", attribute
    )

    response = billing_client.post(
        "/admin/billing/unresolved/7d3b/attribute", json={"pubkey": PUBKEY}
    )

    assert response.status_code == 200
    assert attribute.await_args.kwargs["subscription_id"] == "7d3b"
    assert attribute.await_args.kwargs["pubkey"] == PUBKEY
    assert attribute.await_args.kwargs["acting_pubkey"] == caller.pubkey
    assert response.json()["resolution"] == "attributed"


def test_dismissing_names_the_admin_who_did_it(billing_client, caller, monkeypatch):
    dismiss = AsyncMock(return_value=_resolution(resolution="dismissed"))
    monkeypatch.setattr(
        "app.routers.admin.billing.router.dismiss_unresolved_subscription", dismiss
    )

    response = billing_client.post("/admin/billing/unresolved/7d3b/dismiss")

    assert response.status_code == 200
    assert dismiss.await_args.kwargs["acting_pubkey"] == caller.pubkey
    assert response.json() == {
        "subscription_id": "7d3b",
        "resolution": "dismissed",
        "pubkey": None,
        "applied": False,
        "events_settled": 1,
    }


def test_something_that_is_not_a_pubkey_never_reaches_the_grant(
    billing_client, monkeypatch
):
    attribute = AsyncMock()
    monkeypatch.setattr(
        "app.routers.admin.billing.router.attribute_unresolved_subscription", attribute
    )

    response = billing_client.post(
        "/admin/billing/unresolved/7d3b/attribute", json={"pubkey": "nostr:jane"}
    )

    assert response.status_code == 422
    attribute.assert_not_awaited()


def test_an_unreachable_flash_is_told_apart_from_a_dead_key(
    billing_client, monkeypatch
):
    """Same distinction the read path makes: retrying helps for one and never
    helps for the other."""
    from app.core.flash import FlashCredentialError, FlashUnavailable

    for failure, expected in (
        (FlashUnavailable("socket timed out"), 503),
        (FlashCredentialError("Flash refused our credentials (401)"), 502),
    ):
        monkeypatch.setattr(
            "app.routers.admin.billing.router.attribute_unresolved_subscription",
            AsyncMock(side_effect=failure),
        )
        response = billing_client.post(
            "/admin/billing/unresolved/7d3b/attribute", json={"pubkey": PUBKEY}
        )
        assert response.status_code == expected
        assert isinstance(response.json()["detail"], str)


def test_a_signup_cannot_be_resolved_without_billing_access(client, monkeypatch):
    monkeypatch.setattr(settings, "billing_admin_whitelisted_pubkeys", OTHER)
    wl.init_billing_admin_whitelist()

    assert (
        client.post(
            "/admin/billing/unresolved/7d3b/attribute", json={"pubkey": PUBKEY}
        ).status_code
        == 403
    )
    assert client.post("/admin/billing/unresolved/7d3b/dismiss").status_code == 403


# ---------------------------------------------------------------------------
# How each subscriber pays
# ---------------------------------------------------------------------------
def _roster_row(**overrides):
    from app.schemas.schemas import BillingSubscriptionItem

    return BillingSubscriptionItem(
        **{
            "pubkey": PUBKEY,
            "flash_status": "active",
            "flash_subscription_id": "7d3b",
            "flash_service_id": "9c1e",
            "flash_plan_id": "4f2a",
            "scheduling_source": "billing",
            "billing_blocked": False,
            **overrides,
        }
    )


def _roster(monkeypatch, rows, *, accepts=("amt_ln",), methods=None):
    """The roster endpoint with its page supplied and Flash's two reads stubbed
    at their own edges, so the resolution rule itself stays under test."""
    from fastapi_pagination import Page

    from app.core.flash import FlashPlan
    from app.schemas.schemas import BillingSubscriptionItem
    from app.services import payment_method_service

    monkeypatch.setattr(
        "app.routers.admin.billing.router.paginate",
        AsyncMock(
            return_value=Page[BillingSubscriptionItem](
                items=list(rows), total=len(rows), page=1, size=50, pages=1
            )
        ),
    )
    monkeypatch.setattr(
        payment_method_service,
        "read_plans_for_services",
        AsyncMock(
            return_value={
                ("9c1e", "4f2a"): FlashPlan(
                    id="4f2a",
                    service_id="9c1e",
                    name="Monthly",
                    description=None,
                    amount_minor=200,
                    currency="USD",
                    billing_interval="monthly",
                    sort_order=0,
                    features=None,
                    not_included=None,
                    status="active",
                    signup_url="https://flash.example/s/9c1e/4f2a",
                    acceptance_methods=tuple(accepts),
                )
            }
        ),
    )
    monkeypatch.setattr(
        payment_method_service,
        "read_acceptance_methods",
        AsyncMock(
            return_value=dict(
                methods if methods is not None else {"amt_ln": "lightning"}
            )
        ),
    )


def test_the_roster_says_how_each_subscriber_pays(billing_client, monkeypatch):
    _roster(monkeypatch, [_roster_row()])

    items = billing_client.get("/admin/billing/subscriptions").json()["items"]

    assert items[0]["payment_method"] == "lightning"


def test_the_roster_says_nothing_where_the_plan_takes_both(
    billing_client, monkeypatch
):
    """The one answer an admin must never be shown is a plausible one. A plan
    accepting Lightning and card does not say which of them this person used."""
    _roster(
        monkeypatch,
        [_roster_row()],
        accepts=("amt_ln", "amt_card"),
        methods={"amt_ln": "lightning", "amt_card": "card"},
    )

    items = billing_client.get("/admin/billing/subscriptions").json()["items"]

    assert items[0]["payment_method"] is None


def test_the_roster_names_the_flash_plan_it_resolved_that_from(
    billing_client, monkeypatch
):
    """Served beside the subscription id, so an operator checking our answer
    against Flash's dashboard has the ids Flash names things by."""
    _roster(monkeypatch, [_roster_row()])

    items = billing_client.get("/admin/billing/subscriptions").json()["items"]

    assert items[0]["flash_service_id"] == "9c1e"
    assert items[0]["flash_plan_id"] == "4f2a"
