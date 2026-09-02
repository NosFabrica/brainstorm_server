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
    """A mapping row: which Flash plan grants what, and whether we sell it.

    Nothing priced — price, name, period, ordering and copy are Flash's.
    """
    return SimpleNamespace(
        **{
            "id": 1,
            "flash_service_id": "9c1e",
            "flash_plan_id": "4f2a",
            "scheduling_id": PAID_POLICY.id,
            "is_active": True,
            **overrides,
        }
    )


def _flash_plan(**overrides):
    from app.core.flash import FlashPlan

    return FlashPlan(
        **{
            "id": "4f2a",
            "service_id": "9c1e",
            "name": "Monthly",
            "description": None,
            "amount_minor": 200,
            "currency": "USD",
            "billing_interval": "monthly",
            "sort_order": 0,
            "features": None,
            "not_included": None,
            "status": "active",
            "signup_url": "https://flash.example/subscriptions/signup/9c1e/4f2a",
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


def _stub_flash(monkeypatch, flash, flash_error):
    """Stand in for the one Flash read both surfaces make, in its own shape:
    every plan across the services asked for, keyed as a mapping names one."""
    rows = [_flash_plan()] if flash is None else flash
    monkeypatch.setattr(
        view_svc,
        "read_plans_for_services",
        AsyncMock(
            return_value={(row.service_id, row.id): row for row in rows},
            side_effect=flash_error,
        ),
    )


def _stub_view(
    monkeypatch,
    *,
    policy=FREE_POLICY,
    row=None,
    plan=None,
    flash=None,
    flash_error=None,
    acceptance_methods=None,
):
    _stub_flash(monkeypatch, flash, flash_error)
    # Stubbed at Flash's own edge, so the resolution rule itself stays under
    # test here rather than being replaced by an answer.
    from app.services import payment_method_service

    monkeypatch.setattr(
        payment_method_service,
        "read_acceptance_methods",
        AsyncMock(return_value=dict(acceptance_methods or {})),
    )
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
            "portal_url": "https://flash.example/subscriptions/portal/9c1e",
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
        "payment_method",
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


def test_a_user_who_bought_nothing_is_shown_no_payment_method(
    subscription_client, monkeypatch
):
    """They pay nothing, so there is nothing to say — and the field is absent
    for the same reason it is absent from an ambiguous plan, not a special
    case anybody has to remember."""
    _stub_view(monkeypatch)

    assert subscription_client.get("/user/subscription").json()["data"][
        "payment_method"
    ] is None


def test_nothing_on_the_wire_carries_a_tier_or_a_rail(
    subscription_client, monkeypatch
):
    """No tier string, and not the stored `rail` column either: nothing Flash
    reports per subscription can fill it, so what goes out is resolved from the
    plan's acceptance methods on read instead."""
    monkeypatch.setattr(settings, "flash_enabled", True)
    _stub_view(monkeypatch, policy=PAID_POLICY, row=_row(), plan=_plan())

    body = subscription_client.get("/user/subscription").text

    assert '"tier"' not in body
    assert '"rail"' not in body


def test_a_subscriber_on_a_plan_taking_one_method_is_told_which(
    subscription_client, monkeypatch
):
    monkeypatch.setattr(settings, "flash_enabled", True)
    _stub_view(
        monkeypatch,
        policy=PAID_POLICY,
        row=_row(),
        plan=_plan(),
        flash=[_flash_plan(acceptance_methods=("amt_ln",))],
        acceptance_methods={"amt_ln": "lightning"},
    )

    data = subscription_client.get("/user/subscription").json()["data"]

    assert data["payment_method"] == "lightning"


def test_a_subscriber_on_a_plan_taking_both_is_told_nothing(
    subscription_client, monkeypatch
):
    """They paid one way or the other and Flash does not say which. The card
    shows no payment-method row at all rather than picking one."""
    monkeypatch.setattr(settings, "flash_enabled", True)
    _stub_view(
        monkeypatch,
        policy=PAID_POLICY,
        row=_row(),
        plan=_plan(),
        flash=[_flash_plan(acceptance_methods=("amt_ln", "amt_card"))],
        acceptance_methods={"amt_ln": "lightning", "amt_card": "card"},
    )

    data = subscription_client.get("/user/subscription").json()["data"]

    assert data["payment_method"] is None


def test_a_flash_outage_costs_the_payment_method_rather_than_inventing_one(
    subscription_client, monkeypatch
):
    """Same rule as the price: a card that cannot say how they pay must not
    therefore say the wrong thing."""
    monkeypatch.setattr(settings, "flash_enabled", True)
    _stub_view(
        monkeypatch, policy=PAID_POLICY, row=_row(), plan=_plan(), flash=[]
    )

    data = subscription_client.get("/user/subscription").json()["data"]

    assert data["payment_method"] is None


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
    assert data["manage_url"] == "https://flash.example/subscriptions/portal/9c1e"


def test_the_manage_link_is_the_one_flash_gave_not_one_we_spelled(
    subscription_client, monkeypatch
):
    """Served verbatim from Flash's answer, so a portal they move is a portal
    we follow. The two agree today, which is exactly why only a link Flash
    could not have been guessed into proves which one is being served."""
    monkeypatch.setattr(settings, "flash_enabled", True)
    _stub_view(
        monkeypatch,
        policy=PAID_POLICY,
        row=_row(portal_url="https://billing.flash.example/manage/9c1e"),
        plan=_plan(),
    )

    data = subscription_client.get("/user/subscription").json()["data"]

    assert data["manage_url"] == "https://billing.flash.example/manage/9c1e"


def test_a_subscription_flash_gave_no_portal_for_offers_no_manage_link(
    subscription_client, monkeypatch
):
    """A link is Flash's to supply. With none, the honest answer is none — a
    guess would send someone mid-cancellation to a page that cannot help."""
    monkeypatch.setattr(settings, "flash_enabled", True)
    _stub_view(
        monkeypatch, policy=PAID_POLICY, row=_row(portal_url=None), plan=_plan()
    )

    data = subscription_client.get("/user/subscription").json()["data"]

    assert data["manage_url"] is None


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


def test_the_plan_is_the_one_they_bought_priced_by_flash(
    subscription_client, monkeypatch
):
    """Which plan is read through `billing_plan_id`, not looked up by policy —
    someone on the daily rehearsal plan is not shown the monthly price. What it
    costs is Flash's answer about that plan, not a value we transcribed."""
    monkeypatch.setattr(settings, "flash_enabled", True)
    _stub_view(
        monkeypatch,
        policy=PAID_POLICY,
        row=_row(),
        plan=_plan(),
        flash=[_flash_plan(amount_minor=10, billing_interval="daily")],
    )

    plan = subscription_client.get("/user/subscription").json()["data"]["plan"]

    assert plan == {
        "amount_minor": 10,
        "currency": "USD",
        "is_active": True,
        "billing_interval": "daily",
    }


def test_a_subscriber_still_sees_their_plan_when_flash_cannot_be_read(
    subscription_client, monkeypatch
):
    """Their entitlement does not depend on this call and neither should the
    page. What we still know — that they hold a paid policy, and that we still
    sell the plan — is reported; the price is simply absent."""
    monkeypatch.setattr(settings, "flash_enabled", True)
    _stub_view(
        monkeypatch, policy=PAID_POLICY, row=_row(), plan=_plan(), flash=[]
    )

    data = subscription_client.get("/user/subscription").json()["data"]

    assert data["status"] == "active"
    assert data["plan"] == {
        "amount_minor": None,
        "currency": None,
        "is_active": True,
        "billing_interval": None,
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
def _stub_plans(monkeypatch, *, policies, plans, flash=None, flash_error=None):
    monkeypatch.setattr(settings, "flash_enabled", True)
    monkeypatch.setattr(settings, "flash_checkout_redirect_url", "")
    monkeypatch.setattr(settings, "frontend_url", "https://app.example.com")
    monkeypatch.setattr(
        view_svc, "select_public_scheduling_on_db", AsyncMock(return_value=policies)
    )
    listed = AsyncMock(return_value=plans)
    monkeypatch.setattr(view_svc, "select_billing_plans_on_db", listed)
    _stub_flash(monkeypatch, flash, flash_error)
    return listed


def test_no_billing_configured_lists_nothing(subscription_client, monkeypatch):
    """The empty list IS the "no billing here" signal the UI hides on."""
    monkeypatch.setattr(settings, "flash_enabled", False)

    body = subscription_client.get("/billing/plans").json()

    assert body["data"] == {"plans": []}


def test_a_priced_row_is_flashs_answer_beside_our_own_mapping(monkeypatch):
    """Everything on the card comes from Flash except the two things Flash
    cannot know: which policy buying it grants, and that we sell it at all."""
    _stub_plans(
        monkeypatch,
        policies=[PAID_POLICY],
        plans=[_plan()],
        flash=[
            _flash_plan(
                name="Monthly",
                description="Best value",
                amount_minor=100,
                currency="SAT",
                billing_interval="monthly",
                features=["weekly recalculation"],
                not_included=["priority support"],
            )
        ],
    )

    row = asyncio.run(view_svc.list_billing_plans(AsyncMock())).plans[0]

    assert row.plan_name == "Monthly"
    assert row.description == "Best value"
    assert row.amount_minor == 100
    assert row.currency == "SAT"
    assert row.billing_interval == "monthly"
    assert row.features == ["weekly recalculation"]
    assert row.not_included == ["priority support"]
    # Ours, both of them.
    assert row.policy_id == PAID_POLICY.id
    assert row.policy_name == "Paid Staging Flash Test"
    assert row.schedule_interval_seconds == 604800


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
    # Flash's own signup URL, not one spelled out of our base URL and two ids.
    assert paid.checkout_url.startswith(
        "https://flash.example/subscriptions/signup/9c1e/4f2a"
    )
    assert "redirect_uri=https%3A%2F%2Fapp.example.com%2Fbilling%2Freturn" in (
        paid.checkout_url
    )


def test_the_redirect_survives_whatever_shape_flashs_signup_url_arrives_in(
    monkeypatch,
):
    """We no longer write this URL, so we no longer know its shape. Joined by
    hand, a fragment would swallow `redirect_uri` — and a swallowed one fails
    Flash's exact match, which stops the checkout rather than degrading it."""
    _stub_plans(
        monkeypatch,
        policies=[PAID_POLICY],
        plans=[_plan()],
        flash=[
            _flash_plan(
                signup_url="https://flash.example/subscriptions/signup/9c1e/4f2a"
                "?utm=card#plans"
            )
        ],
    )

    url = asyncio.run(view_svc.list_billing_plans(AsyncMock())).plans[0].checkout_url

    assert url == (
        "https://flash.example/subscriptions/signup/9c1e/4f2a"
        "?utm=card&redirect_uri=https%3A%2F%2Fapp.example.com%2Fbilling%2Freturn"
        "#plans"
    )


def test_the_free_row_has_no_flash_plan_and_keeps_its_local_name(monkeypatch):
    """Nothing sells it, so there is no Flash plan to name it. The policy's own
    name is the only name it has ever had."""
    _stub_plans(monkeypatch, policies=[FREE_POLICY], plans=[])

    free = asyncio.run(view_svc.list_billing_plans(AsyncMock())).plans[0]

    assert free.plan_name is None
    assert free.policy_name == "Free"
    assert free.billing_interval is None
    assert free.features is None


def test_two_plans_on_one_policy_are_two_rows_granting_the_same_thing(monkeypatch):
    """Staging sells one paid policy through two live mappings — a $0.10/day
    rehearsal plan beside the real $2.00 one. Both are buyable, both grant the
    same policy, and neither hides the other."""
    _stub_plans(
        monkeypatch,
        policies=[FREE_POLICY, PAID_POLICY],
        plans=[
            _plan(id=1),
            _plan(id=2, flash_plan_id="019e"),
        ],
        flash=[
            _flash_plan(amount_minor=10, billing_interval="daily", sort_order=0),
            _flash_plan(id="019e", amount_minor=200, sort_order=1),
        ],
    )

    plans = asyncio.run(view_svc.list_billing_plans(AsyncMock())).plans

    paid = [row for row in plans if not row.is_default]
    assert [row.amount_minor for row in paid] == [10, 200]
    assert {row.policy_id for row in paid} == {PAID_POLICY.id}
    assert {row.policy_name for row in paid} == {"Paid Staging Flash Test"}


def test_the_order_is_the_answer_default_first_then_flashs_own(monkeypatch):
    """The client renders the array as given and never sorts. Ordering stopped
    being ours with the rest of it: it is Flash's `sortOrder` now, and the free
    row has a placement rule rather than a sort field."""
    _stub_plans(
        monkeypatch,
        policies=[PAID_POLICY, FREE_POLICY],
        plans=[_plan(id=1), _plan(id=2, flash_plan_id="019e")],
        flash=[
            _flash_plan(amount_minor=10, sort_order=9),
            _flash_plan(id="019e", amount_minor=200, sort_order=1),
        ],
    )

    plans = asyncio.run(view_svc.list_billing_plans(AsyncMock())).plans

    assert plans[0].is_default is True
    assert [row.amount_minor for row in plans[1:]] == [200, 10]


def test_a_mapping_flash_no_longer_returns_leaves_the_page_standing(monkeypatch):
    """A plan deleted in Flash cannot be priced, so it cannot be sold — but it
    must not take the rest of the page with it."""
    _stub_plans(
        monkeypatch,
        policies=[FREE_POLICY, PAID_POLICY],
        plans=[_plan(id=1), _plan(id=2, flash_plan_id="gone")],
        flash=[_flash_plan()],
    )

    plans = asyncio.run(view_svc.list_billing_plans(AsyncMock())).plans

    assert [row.plan_name for row in plans] == [None, "Monthly"]


def test_a_service_flash_does_not_hold_does_not_take_the_pricing_page_down(
    subscription_client, monkeypatch
):
    """A service id an operator mistyped is theirs to fix, and the log names
    it. It is not a reason to refuse everyone a pricing page: the mapping goes
    unsold, and every row that does not depend on it still renders."""
    _stub_plans(
        monkeypatch,
        policies=[FREE_POLICY, PAID_POLICY],
        plans=[_plan()],
        flash=[],
    )

    response = subscription_client.get("/billing/plans")

    assert response.status_code == 200
    assert len(response.json()["data"]["plans"]) == 1


def test_a_plan_we_cannot_price_is_not_offered_for_sale(monkeypatch):
    """Flash sends amounts as strings in minor units. One we cannot read is
    unknown, not zero — and zero renders as "Free" on a public page, which is
    the one wrong answer a pricing page must never give. Unsellable, like a
    plan Flash no longer returns."""
    _stub_plans(
        monkeypatch,
        policies=[FREE_POLICY, PAID_POLICY],
        plans=[_plan()],
        flash=[_flash_plan(amount_minor=None)],
    )

    plans = asyncio.run(view_svc.list_billing_plans(AsyncMock())).plans

    assert [row.policy_id for row in plans] == [FREE_POLICY.id]


def test_a_plan_with_no_signup_url_of_flashs_own_is_not_offered_for_sale(monkeypatch):
    """We no longer keep a way to spell a checkout URL ourselves, so a plan
    Flash gives no signup link for is a plan with no Subscribe button. Withdrawn
    rather than priced with nowhere to go, exactly like one we cannot price."""
    _stub_plans(
        monkeypatch,
        policies=[FREE_POLICY, PAID_POLICY],
        plans=[_plan()],
        flash=[_flash_plan(signup_url=None)],
    )

    plans = asyncio.run(view_svc.list_billing_plans(AsyncMock())).plans

    assert [row.policy_id for row in plans] == [FREE_POLICY.id]


def test_an_unreachable_flash_leaves_the_page_standing_too(monkeypatch):
    """With no cached copy at all there is nothing to price a paid row with.
    The page renders what it can — which is the policies nobody sells. A
    policy we DO sell is left out rather than falling through to a free row:
    omitting it costs a sale, and "Free" on a paid tier is a price we would
    have to honour."""
    _stub_plans(
        monkeypatch,
        policies=[FREE_POLICY, PAID_POLICY],
        plans=[_plan()],
        flash=[],
    )

    plans = asyncio.run(view_svc.list_billing_plans(AsyncMock())).plans

    assert [row.policy_id for row in plans] == [FREE_POLICY.id]
    assert all(row.checkout_url is None for row in plans)


def test_flash_is_asked_once_per_service_not_once_per_plan(monkeypatch):
    """Two mappings on one service are one read. The cache would absorb the
    second, but a read per row would still be a read per row on a cold page."""
    _stub_plans(
        monkeypatch,
        policies=[PAID_POLICY],
        plans=[_plan(id=1), _plan(id=2, flash_plan_id="019e")],
        flash=[_flash_plan(), _flash_plan(id="019e")],
    )

    asyncio.run(view_svc.list_billing_plans(AsyncMock()))

    assert view_svc.read_plans_for_services.await_count == 1


def test_a_policy_that_is_not_public_never_reaches_the_pricing_page(monkeypatch):
    """An operator's internal policy must not leak onto a public page, and
    neither must the plans that sell it."""
    _stub_plans(
        monkeypatch,
        policies=[FREE_POLICY],
        plans=[_plan(), _plan(id=2, flash_plan_id="019e", scheduling_id=99)],
        flash=[_flash_plan(), _flash_plan(id="019e")],
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
    """Copy is Flash's now, which makes it no safer: it still crosses the wire
    as data the client escapes, never markup the pricing page would parse."""
    _stub_plans(
        monkeypatch,
        policies=[PAID_POLICY],
        plans=[_plan()],
        flash=[_flash_plan(description="<b>best value</b>")],
    )

    row = asyncio.run(view_svc.list_billing_plans(AsyncMock())).plans[0]

    assert row.description == "<b>best value</b>"


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
