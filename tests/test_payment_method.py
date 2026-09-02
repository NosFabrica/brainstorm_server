"""How a subscription is paid for, and the far larger set of cases where we
refuse to say.

Flash publishes no payment method on a subscription — `paymentInstrumentId` is
documented as deliberately withheld — so the only fact available is the set of
methods its PLAN accepts. A plan accepting exactly one is an answer; a plan
accepting two is not, and must render as nothing rather than as the first of
them.
"""

import asyncio
from unittest.mock import AsyncMock

import pytest

from app.core.flash import FlashPlan
from app.services import payment_method_service as svc

METHODS = {"amt_ln": "lightning", "amt_card": "card"}


def _plan(plan_id="4f2a", accepts=("amt_ln",)):
    return FlashPlan(
        id=plan_id,
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
        signup_url="https://flash.example/subscriptions/signup/9c1e/4f2a",
        acceptance_methods=tuple(accepts),
    )


@pytest.fixture
def flash_methods(monkeypatch):
    reader = AsyncMock(return_value=dict(METHODS))
    monkeypatch.setattr(svc, "read_acceptance_methods", reader)
    return reader


def test_a_plan_accepting_one_method_says_how_it_is_paid_for(flash_methods):
    found = asyncio.run(svc.read_payment_methods([_plan(accepts=("amt_card",))]))

    assert found == {("9c1e", "4f2a"): "card"}


def test_a_plan_accepting_two_methods_says_nothing(flash_methods):
    """The subscriber used one of them and Flash does not report which. A
    payment method guessed on a billing page is worse than no payment method."""
    found = asyncio.run(svc.read_payment_methods([_plan(accepts=("amt_ln", "amt_card"))]))

    assert found == {}


def test_a_plan_accepting_nothing_we_recognise_says_nothing(flash_methods):
    found = asyncio.run(
        svc.read_payment_methods([_plan(accepts=()), _plan("b2", accepts=("amt_new",))])
    )

    assert found == {}


def test_each_plan_is_answered_on_its_own(flash_methods):
    """One ambiguous plan must not cost an unambiguous one its answer."""
    found = asyncio.run(
        svc.read_payment_methods(
            [_plan("solo", accepts=("amt_ln",)), _plan("both", accepts=("amt_ln", "amt_card"))]
        )
    )

    assert found == {("9c1e", "solo"): "lightning"}


def test_nothing_to_resolve_does_not_ask_flash(flash_methods):
    """The unauthenticated pricing page and free users both land here with no
    plans at all; neither should put a Flash call in front of a render."""
    assert asyncio.run(svc.read_payment_methods([])) == {}
    assert flash_methods.await_count == 0
