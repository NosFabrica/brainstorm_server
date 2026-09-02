"""The UI-facing read side: status translation, the response-shape contract,
and the plans page. Slice 08, reshaped by slice 17.

There is no tier string on either endpoint. A subscriber's tier is the policy
they hold, found in one hop with no tiebreak, and paid-vs-free is `is_default`
rather than a name a client has to recognise. The contract test is what catches
the silent traps: enveloped under ``data``, every field present, ISO dates with
an explicit Z.
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

FREE_POLICY = SimpleNamespace(
    id=1, name="Free", schedule_interval_seconds=5184000, is_default=True
)
PAID_POLICY = SimpleNamespace(
    id=7, name="Paid Staging Flash Test", schedule_interval_seconds=604800,
    is_default=False,
)


def _plan(**overrides):
    return SimpleNamespace(
        **{
            "id": 1,
            "flash_service_id": "9c1e",
            "flash_plan_id": "4f2a",
            "scheduling_id": PAID_POLICY.id,
            "amount_minor": 200,
            "currency": "USD",
            "billing_period_unit": "month",
            "billing_period_count": 1,
            "sort_order": 0,
            "blurb": None,
            "includes": None,
            "excludes": None,
            "is_active": True,
            **overrides,
        }
    )


# ---------------------------------------------------------------------------
# Status translation — every row of the table
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    ("flash_status", "is_default", "expected"),
    [
        ("active", False, "active"),
        ("trial", False, "active"),
        ("pending", True, "pending"),
        ("past_due", False, "grace"),
        ("paused", True, "canceled"),
        ("canceled", False, "canceled"),
        ("expired", True, "canceled"),
    ],
)
def test_every_documented_status_maps_to_exactly_one_ui_status(
    flash_status, is_default, expected
):
    assert _translate(flash_status, is_default=is_default) == expected


def test_an_unrecognised_status_reports_what_the_policy_says():
    """Flash's set is open. A new status moves nobody: the answer comes from
    the scheduling assignment, which is what they actually receive."""
    assert _translate("hibernating", is_default=False) == "active"
    assert _translate("hibernating", is_default=True) == "none"


def test_no_billing_row_reads_from_the_policy():
    # A comped user (non-default policy, no row) reads as active; everyone else
    # is on the default and has bought nothing.
    assert _translate(None, is_default=False) == "active"
    assert _translate(None, is_default=True) == "none"


def test_status_is_derived_from_is_default_not_from_a_name():
    """The old derivation compared a free-text tier against "free" by equality,
    so a typo read as an active paid subscriber. There is no name to typo now —
    two policies with identical names still answer by what they are."""
    confusing = dict(is_default=True)
    assert _translate("active", **confusing) == "active"  # Flash's word wins
    assert _translate(None, **confusing) == "none"
    assert _translate(None, is_default=False) == "active"


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


def _stub_view(monkeypatch, *, policy=FREE_POLICY, row=None, plan=None):
    monkeypatch.setattr(
        view_svc,
        "get_assigned_scheduling_id_on_db",
        AsyncMock(return_value=None if policy is None else policy.id),
    )
    monkeypatch.setattr(
        view_svc, "get_scheduling_on_db", AsyncMock(return_value=policy)
    )
    monkeypatch.setattr(
        view_svc, "get_default_scheduling_on_db", AsyncMock(return_value=policy)
    )
    monkeypatch.setattr(
        view_svc, "get_user_subscription_on_db", AsyncMock(return_value=row)
    )
    monkeypatch.setattr(
        view_svc, "get_billing_plan_by_id_on_db", AsyncMock(return_value=plan)
    )


def _row(**overrides):
    return SimpleNamespace(
        **{
            "flash_status": "active",
            "current_period_start": NOW,
            "current_period_end": NOW,
            "next_billing_date": NOW,
            "cancel_effective_date": None,
            "billing_plan_id": 1,
            **overrides,
        }
    )


def test_the_contract_shape_for_a_user_with_no_subscription(
    subscription_client, monkeypatch
):
    """Enveloped under `data`; every field present; no tier and no rail."""
    _stub_view(monkeypatch)

    body = subscription_client.get("/user/subscription").json()

    assert set(body["data"].keys()) == {
        "policy",
        "plan",
        "status",
        "current_period_start",
        "current_period_end",
        "next_billing_date",
        "cancel_effective_date",
        "manage_url",
    }
    assert body["data"]["policy"] == {
        "id": 1,
        "name": "Free",
        "schedule_interval_seconds": 5184000,
        "is_default": True,
    }
    assert body["data"]["plan"] is None
    assert body["data"]["status"] == "none"
    assert body["data"]["current_period_end"] is None
    assert body["data"]["cancel_effective_date"] is None
    assert body["data"]["manage_url"] is None


def test_nothing_on_the_wire_carries_a_tier_or_a_rail(
    subscription_client, monkeypatch
):
    """`rail` was structurally always null — Flash exposes no payment method —
    and a permanently-null field invites someone to populate it by inference."""
    monkeypatch.setattr(settings, "flash_enabled", True)
    _stub_view(monkeypatch, policy=PAID_POLICY, row=_row(), plan=_plan())

    body = subscription_client.get("/user/subscription").text

    assert '"tier"' not in body
    assert '"rail"' not in body


def test_the_contract_shape_for_a_paying_subscriber(
    subscription_client, monkeypatch
):
    monkeypatch.setattr(settings, "flash_enabled", True)
    _stub_view(monkeypatch, policy=PAID_POLICY, row=_row(), plan=_plan())

    data = subscription_client.get("/user/subscription").json()["data"]

    assert data["policy"]["name"] == "Paid Staging Flash Test"
    assert data["policy"]["is_default"] is False
    assert data["status"] == "active"
    # ISO 8601 with an explicit UTC marker — both UI components do
    # `new Date(value)`, which reads an offset-less string as LOCAL time.
    assert data["current_period_start"] == "2026-08-25T12:00:00Z"
    assert data["current_period_end"] == "2026-08-25T12:00:00Z"
    assert data["next_billing_date"] == "2026-08-25T12:00:00Z"
    assert data["manage_url"].endswith("/subscriptions/portal/9c1e")


def test_a_boundary_flash_named_as_a_date_goes_out_as_that_date(
    subscription_client, monkeypatch
):
    """The shape survives the round trip, so no viewer's timezone can move the
    day. Only a value carrying a real time is handed over as an instant."""
    monkeypatch.setattr(settings, "flash_enabled", True)
    ends = datetime(2026, 9, 20, 23, 59, 59, 999999)
    _stub_view(
        monkeypatch,
        policy=PAID_POLICY,
        row=_row(
            current_period_start=datetime(2026, 8, 20, 0, 0),
            current_period_end=ends,
            next_billing_date=datetime(2026, 9, 20, 0, 0),
            cancel_effective_date=ends,
        ),
        plan=_plan(),
    )

    data = subscription_client.get("/user/subscription").json()["data"]

    assert data["current_period_start"] == "2026-08-20"
    assert data["current_period_end"] == "2026-09-20"
    assert data["next_billing_date"] == "2026-09-20"
    assert data["cancel_effective_date"] == "2026-09-20"


def test_the_plan_is_what_they_bought_period_included(
    subscription_client, monkeypatch
):
    """Read through `billing_plan_id`, not looked up by policy: someone on the
    daily rehearsal plan is charged $0.10 a day whatever the policy sells for
    now, and the period is a unit and a count so the client can format it."""
    monkeypatch.setattr(settings, "flash_enabled", True)
    _stub_view(
        monkeypatch,
        policy=PAID_POLICY,
        row=_row(),
        plan=_plan(amount_minor=10, billing_period_unit="day", billing_period_count=1),
    )

    plan = subscription_client.get("/user/subscription").json()["data"]["plan"]

    assert plan == {
        "amount_minor": 10,
        "currency": "USD",
        "is_active": True,
        "billing_period_unit": "day",
        "billing_period_count": 1,
    }


def test_a_retired_plan_says_so_without_changing_what_is_received(
    subscription_client, monkeypatch
):
    """Retiring a plan withdraws it from sale and nothing else. The subscriber
    keeps the policy; `plan.is_active` false is what lets the card tell them the
    plan is no longer offered."""
    monkeypatch.setattr(settings, "flash_enabled", True)
    _stub_view(
        monkeypatch, policy=PAID_POLICY, row=_row(), plan=_plan(is_active=False)
    )

    data = subscription_client.get("/user/subscription").json()["data"]

    assert data["policy"]["id"] == PAID_POLICY.id
    assert data["status"] == "active"
    assert data["plan"]["is_active"] is False


def test_a_cancelled_subscription_still_reads_active_and_carries_its_end_date(
    subscription_client, monkeypatch
):
    """Flash reports a cancellation that has not taken effect as `active` with
    a cancelEffectiveDate, and they really are still entitled — so the status
    stays `active` and the date is what tells the UI it will not renew. Reading
    "cancelled" off the status instead would strip a tier they have paid for."""
    monkeypatch.setattr(settings, "flash_enabled", True)
    _stub_view(
        monkeypatch,
        policy=PAID_POLICY,
        row=_row(cancel_effective_date=NOW),
        plan=_plan(),
    )

    data = subscription_client.get("/user/subscription").json()["data"]

    assert data["status"] == "active", "a paid period still running is not cancelled"
    assert data["policy"]["is_default"] is False
    assert data["cancel_effective_date"] == "2026-08-25T12:00:00Z"


def test_the_policy_comes_from_the_assignment_not_the_billing_record(
    subscription_client, monkeypatch
):
    """Billing says paid, the policy write failed: report the default policy —
    visible and complainable, not a tier the scheduler isn't delivering."""
    _stub_view(monkeypatch, policy=FREE_POLICY, row=_row(), plan=_plan())

    data = subscription_client.get("/user/subscription").json()["data"]

    assert data["policy"]["is_default"] is True
    assert data["status"] == "active"


def test_an_unassigned_user_falls_back_to_the_default_policy(monkeypatch):
    """`scheduling_id IS NULL` is not "no policy" — it is the default one, which
    is what the scheduler actually runs them on."""
    monkeypatch.setattr(
        view_svc, "get_assigned_scheduling_id_on_db", AsyncMock(return_value=None)
    )
    monkeypatch.setattr(
        view_svc, "get_scheduling_on_db", AsyncMock(return_value=PAID_POLICY)
    )
    default = AsyncMock(return_value=FREE_POLICY)
    monkeypatch.setattr(view_svc, "get_default_scheduling_on_db", default)

    policy = asyncio.run(view_svc._resolve_policy(AsyncMock(), "a" * 64))

    assert policy is FREE_POLICY
    default.assert_awaited_once()


def test_the_policy_is_one_hop_with_no_plan_lookup(monkeypatch):
    """Two plans can sell one policy — a monthly beside a yearly, or a
    replacement beside the row it retires. The old resolution picked one with an
    `is_active` tiebreak; there is nothing left to tiebreak, because the policy
    is the answer."""
    monkeypatch.setattr(
        view_svc, "get_assigned_scheduling_id_on_db", AsyncMock(return_value=7)
    )
    lookup = AsyncMock(return_value=PAID_POLICY)
    monkeypatch.setattr(view_svc, "get_scheduling_on_db", lookup)
    monkeypatch.setattr(
        view_svc, "get_default_scheduling_on_db", AsyncMock(return_value=FREE_POLICY)
    )

    policy = asyncio.run(view_svc._resolve_policy(AsyncMock(), "a" * 64))

    assert policy is PAID_POLICY
    lookup.assert_awaited_once_with(lookup.await_args.args[0], 7)
    assert not hasattr(view_svc, "get_plan_by_scheduling_id_on_db")


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
    assert response.json()["data"]["policy"]["is_default"] is True


# ---------------------------------------------------------------------------
# The plans page
# ---------------------------------------------------------------------------
def _stub_plans(monkeypatch, *, policies, plans):
    monkeypatch.setattr(settings, "flash_enabled", True)
    monkeypatch.setattr(settings, "flash_checkout_redirect_url", "")
    monkeypatch.setattr(settings, "frontend_url", "https://app.example.com")
    monkeypatch.setattr(
        view_svc, "select_public_scheduling_on_db", AsyncMock(return_value=policies)
    )
    listed = AsyncMock(return_value=plans)
    monkeypatch.setattr(view_svc, "select_billing_plans_on_db", listed)
    return listed


def test_no_billing_configured_lists_nothing(subscription_client, monkeypatch):
    """The empty list IS the "no billing here" signal the UI hides on."""
    monkeypatch.setattr(settings, "flash_enabled", False)

    body = subscription_client.get("/billing/plans").json()

    assert body["data"] == {"plans": []}


def test_plans_carry_live_cadence_and_a_checkout_url_without_ref(monkeypatch):
    _stub_plans(
        monkeypatch, policies=[FREE_POLICY, PAID_POLICY], plans=[_plan()]
    )

    data = asyncio.run(view_svc.list_billing_plans(AsyncMock()))

    free, paid = data.plans
    assert free.is_default is True
    assert free.policy_name == "Free"
    assert free.amount_minor == 0
    assert free.checkout_url is None
    assert free.schedule_interval_seconds == 5184000
    assert paid.policy_id == PAID_POLICY.id
    assert paid.policy_name == "Paid Staging Flash Test"
    assert paid.schedule_interval_seconds == 604800
    assert "ref=" not in paid.checkout_url
    assert paid.checkout_url.startswith(
        settings.flash_base_url.rstrip("/") + "/subscriptions/signup/9c1e/4f2a"
    )
    assert "redirect_uri=https%3A%2F%2Fapp.example.com%2Fbilling%2Freturn" in (
        paid.checkout_url
    )


def test_two_plans_on_one_policy_are_two_rows_granting_the_same_thing(monkeypatch):
    """Staging sells one paid policy through two live mappings — a $0.10/day
    rehearsal plan beside the real $2.00 one. Both are buyable, both grant the
    same policy, and neither hides the other."""
    _stub_plans(
        monkeypatch,
        policies=[FREE_POLICY, PAID_POLICY],
        plans=[
            _plan(id=1, amount_minor=10, billing_period_unit="day", sort_order=0),
            _plan(id=2, flash_plan_id="019e", amount_minor=200, sort_order=1),
        ],
    )

    plans = asyncio.run(view_svc.list_billing_plans(AsyncMock())).plans

    paid = [row for row in plans if not row.is_default]
    assert [row.amount_minor for row in paid] == [10, 200]
    assert {row.policy_id for row in paid} == {PAID_POLICY.id}
    assert {row.policy_name for row in paid} == {"Paid Staging Flash Test"}


def test_the_order_is_the_answer_default_first_then_sort_order(monkeypatch):
    """The client renders the array as given and never sorts, so the free row
    has a placement rule rather than a sort field."""
    _stub_plans(
        monkeypatch,
        policies=[PAID_POLICY, FREE_POLICY],
        plans=[
            _plan(id=2, flash_plan_id="019e", amount_minor=200),
            _plan(id=1, amount_minor=10),
        ],
    )

    plans = asyncio.run(view_svc.list_billing_plans(AsyncMock())).plans

    assert plans[0].is_default is True
    # The repo already ordered by sort_order then id; the service must not
    # reshuffle what it was handed.
    assert [row.amount_minor for row in plans[1:]] == [200, 10]


def test_a_policy_that_is_not_public_never_reaches_the_pricing_page(monkeypatch):
    """An operator's internal policy must not leak onto a public page, and
    neither must the plans that sell it."""
    _stub_plans(
        monkeypatch,
        policies=[FREE_POLICY],
        plans=[_plan(), _plan(id=2, flash_plan_id="019e", scheduling_id=99)],
    )

    plans = asyncio.run(view_svc.list_billing_plans(AsyncMock())).plans

    assert [row.policy_id for row in plans] == [FREE_POLICY.id]


def test_a_public_policy_with_nothing_selling_it_still_renders_free(monkeypatch):
    _stub_plans(monkeypatch, policies=[FREE_POLICY, PAID_POLICY], plans=[])

    plans = asyncio.run(view_svc.list_billing_plans(AsyncMock())).plans

    assert [(row.policy_id, row.amount_minor, row.checkout_url) for row in plans] == [
        (FREE_POLICY.id, 0, None),
        (PAID_POLICY.id, 0, None),
    ]


def test_plan_copy_travels_as_plain_text(monkeypatch):
    """Admin-editable, so it must cross the wire as data the client escapes —
    never markup the pricing page would parse."""
    _stub_plans(
        monkeypatch,
        policies=[PAID_POLICY],
        plans=[
            _plan(
                blurb="<b>best value</b>",
                includes=["weekly recalculation"],
                excludes=["priority support"],
            )
        ],
    )

    row = asyncio.run(view_svc.list_billing_plans(AsyncMock())).plans[0]

    assert row.blurb == "<b>best value</b>"
    assert row.includes == ["weekly recalculation"]
    assert row.excludes == ["priority support"]


def test_the_public_policy_query_builds_against_the_real_model(monkeypatch):
    """Every caller of this mocks the repo layer, so a statement naming a column
    the model does not have would not raise until production."""
    from types import SimpleNamespace as NS

    from app.repos import scheduling_repo

    built = []

    async def _capture(db, statement, name):
        built.append(statement)
        return NS(scalars=lambda: NS(all=lambda: []))

    monkeypatch.setattr(scheduling_repo, "execute_db_statement", _capture)

    asyncio.run(scheduling_repo.select_public_scheduling_on_db(AsyncMock()))

    assert "is_public" in str(built[0])


def test_an_inactive_plan_never_surfaces(monkeypatch):
    listed = _stub_plans(monkeypatch, policies=[FREE_POLICY], plans=[])

    asyncio.run(view_svc.list_billing_plans(AsyncMock()))

    assert listed.await_args.kwargs["only_active"] is True
