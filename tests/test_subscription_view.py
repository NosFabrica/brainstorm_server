"""The UI-facing read side: status translation, the response-shape contract,
and the plans page. Slice 08.

The client's ``normalize()`` degrades anything malformed to "free" with no
error anywhere, so the contract test is what catches the four silent traps:
enveloped under ``data``, exact lowercase literals, every field present, ISO
dates.
"""

import asyncio
from datetime import datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.core.config import settings
from app.core.database import get_db
from app.services import subscription_view_service as view_svc
from app.services.subscription_view_service import _translate

NOW = datetime(2026, 8, 25, 12, 0, 0)


# ---------------------------------------------------------------------------
# Status translation — every row of the table
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    ("flash_status", "tier", "expected"),
    [
        ("active", "priority", "active"),
        ("trial", "priority", "active"),
        ("pending", "free", "pending"),
        ("past_due", "priority", "grace"),
        ("paused", "free", "canceled"),
        ("canceled", "priority", "canceled"),
        ("expired", "free", "canceled"),
    ],
)
def test_every_documented_status_maps_to_exactly_one_ui_status(
    flash_status, tier, expected
):
    assert _translate(flash_status, tier=tier) == expected


def test_an_unrecognised_status_reports_what_the_policy_says():
    """Flash's set is open. A new status moves nobody: the answer comes from
    the scheduling assignment, which is what they actually receive."""
    assert _translate("hibernating", tier="priority") == "active"
    assert _translate("hibernating", tier="free") == "none"


def test_no_billing_row_reads_from_the_policy():
    # A comped user (paid policy, no row) reads as active; everyone else free.
    assert _translate(None, tier="priority") == "active"
    assert _translate(None, tier="free") == "none"


# ---------------------------------------------------------------------------
# The response-shape contract
# ---------------------------------------------------------------------------
@pytest.fixture
def subscription_client(client, monkeypatch):
    from app.api import app

    async def _fake_get_db():
        yield AsyncMock()

    app.dependency_overrides[get_db] = _fake_get_db
    yield client


def _stub_view(
    monkeypatch,
    *,
    tier="free",
    row=None,
    plan=None,
):
    monkeypatch.setattr(
        view_svc,
        "get_assigned_scheduling_id_on_db",
        AsyncMock(return_value=7 if tier != "free" else None),
    )
    monkeypatch.setattr(
        view_svc,
        "get_plan_by_scheduling_id_on_db",
        AsyncMock(
            return_value=SimpleNamespace(subscription_tier=tier)
            if tier != "free"
            else None
        ),
    )
    monkeypatch.setattr(
        view_svc, "get_user_subscription_on_db", AsyncMock(return_value=row)
    )
    monkeypatch.setattr(
        view_svc, "get_billing_plan_by_id_on_db", AsyncMock(return_value=plan)
    )


def test_the_contract_shape_for_a_user_with_no_subscription(
    subscription_client, monkeypatch
):
    """Enveloped under `data`; all five fields present; exact literals."""
    _stub_view(monkeypatch)

    body = subscription_client.get("/user/subscription").json()

    assert set(body["data"].keys()) == {
        "tier",
        "status",
        "current_period_end",
        "rail",
        "manage_url",
    }
    assert body["data"]["tier"] == "free"
    assert body["data"]["status"] == "none"
    assert body["data"]["current_period_end"] is None
    assert body["data"]["rail"] is None
    assert body["data"]["manage_url"] is None


def test_the_contract_shape_for_a_paying_subscriber(
    subscription_client, monkeypatch
):
    monkeypatch.setattr(settings, "flash_enabled", True)
    _stub_view(
        monkeypatch,
        tier="priority",
        row=SimpleNamespace(
            flash_status="active",
            current_period_end=NOW,
            rail=None,
            billing_plan_id=1,
        ),
        plan=SimpleNamespace(flash_service_id="9c1e"),
    )

    data = subscription_client.get("/user/subscription").json()["data"]

    assert data["tier"] == "priority"
    assert data["status"] == "active"
    # ISO 8601 with an explicit UTC marker — both UI components do
    # `new Date(value)`, which reads an offset-less string as LOCAL time.
    assert data["current_period_end"] == "2026-08-25T12:00:00Z"
    assert data["manage_url"].endswith("/subscriptions/portal/9c1e")


def test_tier_comes_from_the_policy_not_the_billing_record(
    subscription_client, monkeypatch
):
    """Billing says paid, the policy write failed: report free — visible and
    complainable, not a promised tier the scheduler isn't delivering."""
    _stub_view(
        monkeypatch,
        tier="free",
        row=SimpleNamespace(
            flash_status="active",
            current_period_end=NOW,
            rail=None,
            billing_plan_id=1,
        ),
    )

    data = subscription_client.get("/user/subscription").json()["data"]

    assert data["tier"] == "free"
    assert data["status"] == "active"


def test_refresh_takes_nothing_from_the_caller_but_their_identity(
    subscription_client, monkeypatch, caller
):
    """A subscription id or ref in the body would be a claim to someone else's
    payment. Only the authenticated pubkey reaches the lookup."""
    monkeypatch.setattr(settings, "flash_enabled", True)
    monkeypatch.setattr(
        "app.routers.user.router.validate_subscription_refresh_allowed", AsyncMock()
    )
    applied = AsyncMock(return_value=SimpleNamespace(applied=True))
    monkeypatch.setattr("app.routers.user.router.apply_entitlement", applied)
    _stub_view(monkeypatch)

    subscription_client.post(
        "/user/subscription/refresh",
        json={"subscription_id": "somebody_elses", "ref": "b" * 64},
    )

    assert applied.await_args.kwargs["external_ref"] == caller.pubkey
    assert applied.await_args.kwargs["subscription_id"] is None


def test_refresh_answers_with_what_we_hold_when_flash_is_unreachable(
    subscription_client, monkeypatch
):
    from app.core.flash import FlashUnavailable

    monkeypatch.setattr(settings, "flash_enabled", True)
    monkeypatch.setattr(
        "app.routers.user.router.validate_subscription_refresh_allowed", AsyncMock()
    )
    monkeypatch.setattr(
        "app.routers.user.router.apply_entitlement",
        AsyncMock(side_effect=FlashUnavailable("down")),
    )
    _stub_view(monkeypatch)

    response = subscription_client.post("/user/subscription/refresh")

    assert response.status_code == 200
    assert response.json()["data"]["tier"] == "free"


def test_a_hidden_or_retired_plan_still_names_its_tier(monkeypatch):
    """`is_active` hides a plan from the pricing page; it must not strip the
    tier from someone already scheduled on it (the Layer-2 test plan is
    `is_active = false` by design)."""
    monkeypatch.setattr(
        view_svc, "get_assigned_scheduling_id_on_db", AsyncMock(return_value=7)
    )
    lookup = AsyncMock(
        return_value=SimpleNamespace(subscription_tier="priority")
    )
    monkeypatch.setattr(view_svc, "get_plan_by_scheduling_id_on_db", lookup)

    tier = asyncio.run(view_svc._resolve_tier(AsyncMock(), "a" * 64))

    assert tier == "priority"
    # The lookup is by scheduling id alone — no is_active narrowing here.
    lookup.assert_awaited_once()


# ---------------------------------------------------------------------------
# The plans page
# ---------------------------------------------------------------------------
def test_no_billing_configured_lists_nothing(subscription_client, monkeypatch):
    """The empty list IS the "no billing here" signal the UI hides on."""
    monkeypatch.setattr(settings, "flash_enabled", False)

    body = subscription_client.get("/billing/plans").json()

    assert body["data"] == {"plans": []}


def test_plans_carry_live_cadence_and_a_checkout_url_without_ref(monkeypatch):
    monkeypatch.setattr(settings, "flash_enabled", True)
    monkeypatch.setattr(settings, "flash_checkout_redirect_url", "")
    monkeypatch.setattr(settings, "frontend_url", "https://app.example.com")
    monkeypatch.setattr(
        view_svc,
        "get_default_scheduling_on_db",
        AsyncMock(return_value=SimpleNamespace(schedule_interval_seconds=604800)),
    )
    monkeypatch.setattr(
        view_svc,
        "select_billing_plans_on_db",
        AsyncMock(
            return_value=[
                SimpleNamespace(
                    subscription_tier="priority",
                    amount_minor=200,
                    currency="USD",
                    scheduling_id=7,
                    flash_service_id="9c1e",
                    flash_plan_id="4f2a",
                )
            ]
        ),
    )
    monkeypatch.setattr(
        view_svc,
        "get_scheduling_on_db",
        AsyncMock(return_value=SimpleNamespace(schedule_interval_seconds=86400)),
    )

    data = asyncio.run(view_svc.list_billing_plans(AsyncMock()))

    free, priority = data.plans
    assert free.checkout_url is None
    assert free.schedule_interval_seconds == 604800
    assert priority.schedule_interval_seconds == 86400
    assert "ref=" not in priority.checkout_url
    assert priority.checkout_url.startswith(
        settings.flash_base_url.rstrip("/") + "/subscriptions/signup/9c1e/4f2a"
    )
    assert "redirect_uri=https%3A%2F%2Fapp.example.com%2Fbilling%2Freturn" in (
        priority.checkout_url
    )


def test_an_inactive_plan_never_surfaces(monkeypatch):
    monkeypatch.setattr(settings, "flash_enabled", True)
    monkeypatch.setattr(
        view_svc, "get_default_scheduling_on_db", AsyncMock(return_value=None)
    )
    listed = AsyncMock(return_value=[])
    monkeypatch.setattr(view_svc, "select_billing_plans_on_db", listed)

    asyncio.run(view_svc.list_billing_plans(AsyncMock()))

    assert listed.await_args.kwargs["only_active"] is True
