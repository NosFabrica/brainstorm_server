"""Reading subscription state from Flash.

The distinction this file exists to protect: "Flash says they are not a
subscriber" is a fact we act on, while "we could not ask Flash" is not. Confusing
the two would revoke a paying user because a socket timed out.
"""

import asyncio
import json
from datetime import datetime
from unittest.mock import MagicMock

import httpx
import pytest

from app.core import flash
from app.core.flash import (
    UNKNOWN_LIFECYCLE_POLICY,
    FlashCredentialError,
    FlashRefused,
    FlashUnavailable,
    fetch_subscription,
    fetch_subscription_raw,
    parse_flash_timestamp,
    parse_subscription,
)

DUNNING_POLICY = {
    "maxAttempts": 3,
    "retryIntervalDays": 3,
    "gracePeriodDays": 7,
    "cancelAfterFinalFailure": True,
}
CANCELLATION_POLICY = {
    "mode": "end_of_period",
    "minimumCommitmentPeriods": 0,
    "noticePeriodDays": 0,
}

SUBSCRIPTION = {
    "id": "7d3b",
    "status": "active",
    "ref": "a" * 64,
    "subscriberId": "a91c",
    "serviceId": "9c1e",
    "planId": "4f2a",
    "pricingSnapshot": {"planName": "Monthly", "amount": "100", "currency": "USD"},
    "dunningPolicy": DUNNING_POLICY,
    "cancellationPolicy": CANCELLATION_POLICY,
    "currentPeriodStart": "2026-08-20T14:03:11.000Z",
    "currentPeriodEnd": "2026-09-20T14:03:11.000Z",
    "currentPeriodNumber": 1,
    "nextBillingDate": "2026-09-20T14:03:11.000Z",
    "anchorDate": "2026-08-20",
    "trialEndDate": None,
    "cancelEffectiveDate": None,
    "portalUrl": "https://flash.example/subscriptions/portal/9c1e",
}


@pytest.fixture(autouse=True)
def reset_client():
    flash._client = None
    flash._reported_policies.clear()
    yield
    flash._client = None
    flash._reported_policies.clear()


def _transport(handler) -> None:
    """Install a stub transport on the shared client."""
    flash._client = httpx.AsyncClient(transport=httpx.MockTransport(handler))


def _responds(status=200, json=None, calls=None):
    def handler(request: httpx.Request) -> httpx.Response:
        if calls is not None:
            calls.append(request)
        return httpx.Response(status, json=json if json is not None else {})
    return handler


def _fetch(**kwargs):
    return asyncio.run(fetch_subscription(**kwargs))


def _fetch_raw(**kwargs):
    return asyncio.run(fetch_subscription_raw(**kwargs))


# ---------------------------------------------------------------------------
# Looking one up
# ---------------------------------------------------------------------------
def test_a_subscription_can_be_looked_up_by_our_own_reference():
    calls = []
    _transport(_responds(json={"subscriptions": [SUBSCRIPTION]}, calls=calls))

    found = _fetch(ref="a" * 64)

    assert found is not None
    assert found.ref == "a" * 64
    assert calls[0].url.params["ref"] == "a" * 64


def test_a_subscription_is_looked_up_by_flashs_identifier_on_its_own_path():
    """Flash's path lookup, not the filtered list — which no longer accepts an
    id as a filter, so asking that way would return the whole account."""
    calls = []
    _transport(_responds(json={"livemode": True, "subscription": SUBSCRIPTION}, calls=calls))

    found = _fetch(subscription_id="7d3b")

    assert found is not None
    assert found.id == "7d3b"
    assert calls[0].url.path.endswith("/subscriptions/7d3b")
    assert not calls[0].url.params


def test_every_field_flash_sends_is_carried_across():
    found = parse_subscription(SUBSCRIPTION)

    assert found.status == "active"
    assert found.service_id == "9c1e"
    assert found.plan_id == "4f2a"
    assert found.current_period_end == datetime(2026, 9, 20, 14, 3, 11)
    assert found.cancel_effective_date is None
    assert found.portal_url == "https://flash.example/subscriptions/portal/9c1e"


def test_the_policies_that_decide_the_ending_are_carried_across():
    """The two lifecycle rules read these instead of a constant, so they have to
    reach the decision — reporting them and dropping them was the old shape."""
    found = parse_subscription({**SUBSCRIPTION, "dunningAttempts": 2})

    assert found.policy.cancellation_mode == "end_of_period"
    assert found.policy.dunning_max_attempts == 3
    assert found.policy.dunning_attempts == 2
    assert found.policy.dunning_cancels_after_final_failure is True


def test_a_subscription_carrying_no_policies_reads_as_unknown():
    """Absent is not a policy. Every field stays None, and None can only hold."""
    bare = {
        key: value
        for key, value in SUBSCRIPTION.items()
        if key not in ("dunningPolicy", "cancellationPolicy")
    }

    assert parse_subscription(bare).policy == UNKNOWN_LIFECYCLE_POLICY


def test_a_policy_flash_sends_as_the_wrong_type_reads_as_unknown():
    """A string where a number belongs is not a number we may act on."""
    found = parse_subscription(
        _with_policy(
            dunningPolicy={**DUNNING_POLICY, "maxAttempts": "three"},
            cancellationPolicy=[],
        )
    )

    assert found.policy.dunning_max_attempts is None
    assert found.policy.cancellation_mode is None


def test_a_subscription_with_no_portal_of_its_own_offers_none():
    """Absent rather than invented. The alternative is building one from our
    base URL and a service id, which is exactly what a subscriber cannot use
    to reach the right subscription."""
    found = parse_subscription({k: v for k, v in SUBSCRIPTION.items() if k != "portalUrl"})

    assert found.portal_url is None


def test_a_status_we_do_not_recognise_survives_the_parse():
    found = parse_subscription({**SUBSCRIPTION, "status": "hibernating"})

    assert found.status == "hibernating"


# ---------------------------------------------------------------------------
# The pricing Flash snapshotted when they subscribed
# ---------------------------------------------------------------------------
def test_the_price_flash_recorded_at_signup_is_carried_across():
    """What this subscriber is charged, as at the moment they bought. The plan
    catalogue answers a different question — what is on sale today."""
    found = parse_subscription(
        {
            **SUBSCRIPTION,
            "pricingSnapshot": {
                "planName": "Monthly",
                "amount": "200",
                "currency": "USD",
                "billingInterval": "monthly",
            },
        }
    )

    assert found.pricing is not None
    assert found.pricing.amount_minor == 200
    assert found.pricing.currency == "USD"
    assert found.pricing.billing_interval == "monthly"


def test_a_subscription_flash_priced_nowhere_carries_no_pricing():
    """None, so the read side can say nothing rather than quote the plan's
    current list price at somebody who is not charged it."""
    found = parse_subscription(
        {k: v for k, v in SUBSCRIPTION.items() if k != "pricingSnapshot"}
    )

    assert found.pricing is None


def test_a_snapshot_amount_we_cannot_read_is_unknown_not_free():
    """Same rule as the plan catalogue: zero renders as "Free" for someone who
    is being charged, and that is the one reading we cannot take back."""
    found = parse_subscription(
        {**SUBSCRIPTION, "pricingSnapshot": {"amount": "twelve", "currency": "USD"}}
    )

    assert found.pricing is not None
    assert found.pricing.amount_minor is None
    assert found.pricing.currency == "USD"


# ---------------------------------------------------------------------------
# A date with no time on it
#
# Read on the shape of the value, so both readings are already right the day
# Flash stops sending bare dates.
# ---------------------------------------------------------------------------
END_OF_THE_20TH = datetime(2026, 9, 20, 23, 59, 59, 999999)


def test_a_period_ending_on_a_date_runs_to_the_end_of_that_day():
    found = parse_subscription({**SUBSCRIPTION, "currentPeriodEnd": "2026-09-20"})

    assert found.current_period_end == END_OF_THE_20TH


def test_a_cancellation_and_a_trial_dated_to_a_day_last_that_whole_day_too():
    found = parse_subscription(
        {
            **SUBSCRIPTION,
            "cancelEffectiveDate": "2026-09-20",
            "trialEndDate": "2026-09-20",
        }
    )

    assert found.cancel_effective_date == END_OF_THE_20TH
    assert found.trial_end_date == END_OF_THE_20TH


def test_a_period_starting_on_a_date_starts_when_that_day_does():
    """Only deadlines move — nothing measures `now <` against a start."""
    found = parse_subscription(
        {
            **SUBSCRIPTION,
            "currentPeriodStart": "2026-08-20",
            "nextBillingDate": "2026-09-20",
        }
    )

    assert found.current_period_start == datetime(2026, 8, 20, 0, 0)
    assert found.next_billing_date == datetime(2026, 9, 20, 0, 0)


def test_a_deadline_carrying_a_time_is_used_exactly_as_sent():
    found = parse_subscription(
        {**SUBSCRIPTION, "currentPeriodEnd": "2026-09-20T14:03:11Z"}
    )

    assert found.current_period_end == datetime(2026, 9, 20, 14, 3, 11)


def test_a_deadline_at_a_real_midnight_is_not_pushed_to_the_end_of_its_day():
    """The discriminator is the absence of a time, not the value being midnight."""
    found = parse_subscription(
        {**SUBSCRIPTION, "currentPeriodEnd": "2026-09-20T00:00:00Z"}
    )

    assert found.current_period_end == datetime(2026, 9, 20, 0, 0)


def test_a_timestamp_that_is_not_a_deadline_is_never_moved():
    """Webhook event times come through the same parser and are instants."""
    assert parse_flash_timestamp("2026-09-20") == datetime(2026, 9, 20, 0, 0)


# ---------------------------------------------------------------------------
# The account's real policies, against the two we hard-code
#
# Nothing here changes what we do — a difference is reported so it is noticed,
# rather than silently deciding someone's entitlement.
# ---------------------------------------------------------------------------
def _warnings(monkeypatch):
    warned = MagicMock()
    monkeypatch.setattr("app.core.flash.logger.warning", warned)
    return warned


def _with_policy(**policies):
    return {**SUBSCRIPTION, **policies}


def test_the_policies_our_behaviour_already_matches_are_not_reported(monkeypatch):
    warned = _warnings(monkeypatch)
    _transport(_responds(json={"livemode": True, "subscription": SUBSCRIPTION}))

    _fetch(subscription_id="7d3b")

    warned.assert_not_called()


def test_a_cancellation_mode_we_cannot_act_on_is_reported(monkeypatch):
    """`immediate` and `end_of_period` are both decided now. A third mode is
    not, and falls back to the date rule — which may not be what it means."""
    warned = _warnings(monkeypatch)
    _transport(
        _responds(
            json={
                "livemode": True,
                "subscription": _with_policy(
                    cancellationPolicy={"mode": "after_notice_period"}
                ),
            }
        )
    )

    _fetch(subscription_id="7d3b")

    assert warned.call_count == 1
    assert "after_notice_period" in str(warned.call_args)


def test_a_dunning_policy_that_never_retries_is_acted_on_rather_than_reported(
    monkeypatch,
):
    """This used to be a warning ending "Behaviour unchanged". The decision
    reads it now, so there is nothing left to report."""
    warned = _warnings(monkeypatch)
    _transport(
        _responds(
            json={
                "livemode": True,
                "subscription": _with_policy(
                    dunningPolicy={**DUNNING_POLICY, "maxAttempts": 0}
                ),
            }
        )
    )

    _fetch(subscription_id="7d3b")

    assert warned.call_count == 0


def test_a_dunning_policy_that_never_gives_up_is_reported(monkeypatch):
    """Nothing ends a past_due that Flash never cancels, so entitling it once
    entitles it forever."""
    warned = _warnings(monkeypatch)
    _transport(
        _responds(
            json={
                "livemode": True,
                "subscription": _with_policy(
                    dunningPolicy={**DUNNING_POLICY, "cancelAfterFinalFailure": False}
                ),
            }
        )
    )

    _fetch(subscription_id="7d3b")

    assert warned.call_count == 1


def test_one_difference_is_reported_once_however_many_subscribers_carry_it(
    monkeypatch,
):
    """The policy belongs to the account, so every row in a reconcile pass
    carries the same one. Reported per row it would drown the run."""
    warned = _warnings(monkeypatch)
    _transport(
        _responds(
            json={
                "livemode": True,
                "subscription": _with_policy(
                    cancellationPolicy={"mode": "after_notice_period"}
                ),
            }
        )
    )

    _fetch(subscription_id="7d3b")
    _fetch(subscription_id="7d3b")

    assert warned.call_count == 1


def test_a_subscription_carrying_no_policy_at_all_is_not_reported(monkeypatch):
    """Nothing to disagree with. Flash omitting the field is not a difference."""
    warned = _warnings(monkeypatch)
    bare = {
        key: value
        for key, value in SUBSCRIPTION.items()
        if key not in ("dunningPolicy", "cancellationPolicy")
    }
    _transport(_responds(json={"livemode": True, "subscription": bare}))

    _fetch(subscription_id="7d3b")

    warned.assert_not_called()


def test_a_differing_policy_changes_nothing_about_what_is_returned(monkeypatch):
    _warnings(monkeypatch)
    _transport(
        _responds(
            json={
                "livemode": True,
                "subscription": _with_policy(cancellationPolicy={"mode": "immediate"}),
            }
        )
    )

    found = _fetch(subscription_id="7d3b")

    assert found is not None and found.status == "active"


# ---------------------------------------------------------------------------
# Not a subscriber, versus could not ask
# ---------------------------------------------------------------------------
def test_no_such_subscription_is_a_fact_not_a_failure():
    _transport(_responds(json={"subscriptions": []}))

    assert _fetch(ref="nobody") is None


def test_an_id_flash_does_not_know_is_an_answer_not_an_outage():
    """The whole point of the path lookup. Reported as an outage, this refuses
    an admin's attribution for the wrong reason — "try again later" instead of
    "this id is wrong"."""
    _transport(_responds(status=404, json={"error": "not found"}))

    assert _fetch(subscription_id="nosuch") is None


def test_a_timeout_is_never_reported_as_absence():
    """Returning None here would revoke a paying user over a slow socket."""
    def handler(request):
        raise httpx.ConnectTimeout("too slow")

    _transport(handler)

    with pytest.raises(FlashUnavailable):
        _fetch(ref="a" * 64)


def test_a_server_error_is_never_reported_as_absence():
    _transport(_responds(status=503))

    with pytest.raises(FlashUnavailable):
        _fetch(ref="a" * 64)


def test_an_unreadable_body_is_never_reported_as_absence():
    def handler(request):
        return httpx.Response(200, content=b"not json")

    _transport(handler)

    with pytest.raises(FlashUnavailable):
        _fetch(ref="a" * 64)


def test_a_lookup_with_nothing_to_look_up_by_is_refused():
    with pytest.raises(FlashUnavailable):
        _fetch()


# ---------------------------------------------------------------------------
# Which failures are worth retrying
# ---------------------------------------------------------------------------
def test_a_transient_failure_is_retried():
    attempts = []

    def handler(request):
        attempts.append(request)
        if len(attempts) < 3:
            raise httpx.ConnectError("refused")
        return httpx.Response(200, json={"subscriptions": [SUBSCRIPTION]})

    _transport(handler)

    assert _fetch(ref="a" * 64) is not None
    assert len(attempts) == 3


def test_retries_are_bounded():
    attempts = []

    def handler(request):
        attempts.append(request)
        raise httpx.ConnectError("refused")

    _transport(handler)

    with pytest.raises(FlashUnavailable):
        _fetch(ref="a" * 64)
    assert len(attempts) == flash.CONNECT_RETRIES + 1


@pytest.mark.parametrize("status", [401, 403])
def test_a_credential_failure_is_not_retried(status):
    """It will fail identically forever. Looping burns quota and buries the
    one thing a human needs to see."""
    attempts = []
    _transport(_responds(status=status, calls=attempts))

    with pytest.raises(FlashCredentialError):
        _fetch(ref="a" * 64)
    assert len(attempts) == 1


def test_a_credential_failure_is_distinguishable_from_an_outage():
    """Both leave every policy alone, but only one is worth waking someone for."""
    assert issubclass(FlashCredentialError, FlashUnavailable) is False


@pytest.mark.parametrize("status", [400, 404, 422])
def test_a_request_we_got_wrong_is_not_retried(status):
    """On the filtered list, a 404 is our URL being wrong, not an answer about a
    subscriber — the opposite of what it means on a path lookup. Reading it as
    absence here would revoke every payer at once."""
    attempts = []
    _transport(_responds(status=status, calls=attempts))

    with pytest.raises(FlashUnavailable):
        _fetch(ref="a" * 64)
    assert len(attempts) == 1


def test_an_id_flash_does_not_know_is_not_retried_either():
    attempts = []
    _transport(_responds(status=404, calls=attempts))

    assert _fetch(subscription_id="nosuch") is None
    assert len(attempts) == 1


# ---------------------------------------------------------------------------
# Credentials
# ---------------------------------------------------------------------------
def test_the_api_key_is_sent_as_a_bearer_token():
    from app.core.config import settings

    calls = []
    _transport(_responds(json={"subscriptions": []}, calls=calls))

    _fetch(ref="a" * 64)

    assert calls[0].headers["Authorization"] == f"Bearer {settings.flash_api_key}"


def test_no_error_ever_carries_the_api_key():
    from app.core.config import settings

    _transport(_responds(status=401))

    with pytest.raises(FlashCredentialError) as raised:
        _fetch(ref="a" * 64)
    assert settings.flash_api_key not in str(raised.value)


def test_no_error_ever_carries_flashs_response_body():
    """Flash's bodies carry subscriber PII."""
    _transport(_responds(status=500, json={"email": "someone@example.com"}))

    with pytest.raises(FlashUnavailable) as raised:
        _fetch(ref="a" * 64)
    assert "someone@example.com" not in str(raised.value)


# ---------------------------------------------------------------------------
# One connection
# ---------------------------------------------------------------------------
def test_the_client_is_reused_across_calls():
    _transport(_responds(json={"subscriptions": []}))
    first = flash._get_client()

    _fetch(ref="a" * 64)

    assert flash._get_client() is first


def test_closing_releases_the_client():
    _transport(_responds(json={"subscriptions": []}))
    asyncio.run(flash.aclose())

    assert flash._client is None


# ---------------------------------------------------------------------------
# Choosing among several
# ---------------------------------------------------------------------------
def test_asked_by_reference_the_live_subscription_wins_over_a_dead_one():
    dead = {**SUBSCRIPTION, "id": "old", "status": "expired"}
    _transport(_responds(json={"subscriptions": [dead, SUBSCRIPTION]}))

    found = _fetch(ref="a" * 64)

    assert found is not None and found.status == "active"


def test_asked_by_reference_the_longest_running_of_several_wins():
    shorter = {**SUBSCRIPTION, "id": "short", "currentPeriodEnd": "2026-08-26T00:00:00.000Z"}
    _transport(_responds(json={"subscriptions": [SUBSCRIPTION, shorter]}))

    found = _fetch(ref="a" * 64)

    assert found is not None and found.id == "7d3b"


def test_asked_by_reference_the_longest_running_is_decided_on_parsed_dates():
    """Flash sends both bare dates and instants. Compared as text, "2026-09-20"
    sorts below "2026-09-20T00:00:01Z", so a subscription with a whole day left
    loses to one a second past midnight — and the payer is revoked a day early."""
    whole_day = {**SUBSCRIPTION, "id": "whole-day", "currentPeriodEnd": "2026-09-20"}
    just_past_midnight = {
        **SUBSCRIPTION,
        "id": "instant",
        "currentPeriodEnd": "2026-09-20T00:00:01Z",
    }
    _transport(_responds(json={"subscriptions": [just_past_midnight, whole_day]}))

    found = _fetch(ref="a" * 64)

    assert found is not None and found.id == "whole-day"


def test_asked_by_reference_a_subscription_with_no_end_date_never_wins():
    dated = {**SUBSCRIPTION, "id": "dated"}
    undated = {**SUBSCRIPTION, "id": "undated", "currentPeriodEnd": None}
    _transport(_responds(json={"subscriptions": [dated, undated]}))

    found = _fetch(ref="a" * 64)

    assert found is not None and found.id == "dated"


def test_a_subscription_we_cannot_read_is_a_failure_not_an_absence():
    _transport(_responds(json={"subscriptions": ["not an object"]}))

    assert _fetch(ref="a" * 64) is None


def test_a_body_that_is_not_an_object_is_a_failure():
    _transport(_responds(json=["unexpected"]))

    with pytest.raises(FlashUnavailable):
        _fetch(ref="a" * 64)


def test_an_unexpected_error_is_still_reported_as_unavailable():
    """Anything escaping as a bare exception would abort a whole reconcile batch
    instead of being recorded against one subscriber."""
    def handler(request):
        raise RuntimeError("something nobody predicted")

    _transport(handler)

    with pytest.raises(FlashUnavailable):
        _fetch(ref="a" * 64)


# ---------------------------------------------------------------------------
# The raw read — what Flash actually said
# ---------------------------------------------------------------------------
def test_the_raw_read_hands_back_flashs_body_untouched():
    body = {"livemode": True, "subscriptions": [SUBSCRIPTION]}
    _transport(_responds(json=body))

    assert _fetch_raw(ref="a" * 64) == body


def test_the_raw_read_keeps_every_row_the_normal_lookup_would_discard():
    """The multi-row case is exactly what an operator is trying to see: the
    parsed lookup picks one, and that choice is what they are checking."""
    dead = {**SUBSCRIPTION, "id": "old", "status": "expired"}
    _transport(_responds(json={"livemode": True, "subscriptions": [dead, SUBSCRIPTION]}))

    raw = _fetch_raw(ref="a" * 64)

    assert raw is not None
    assert [row["id"] for row in raw["subscriptions"]] == ["old", "7d3b"]


def test_the_raw_read_is_looked_up_by_either_handle():
    calls = []
    _transport(
        _responds(
            json={"subscriptions": [SUBSCRIPTION], "subscription": SUBSCRIPTION},
            calls=calls,
        )
    )

    _fetch_raw(ref="a" * 64)
    _fetch_raw(subscription_id="7d3b")

    assert calls[0].url.params["ref"] == "a" * 64
    assert calls[1].url.path.endswith("/subscriptions/7d3b")


def test_the_raw_read_needs_a_handle_to_look_up_by():
    with pytest.raises(FlashUnavailable):
        _fetch_raw()


def test_the_raw_read_reports_no_such_subscription_as_absence():
    _transport(_responds(json={"livemode": True, "subscriptions": []}))

    assert _fetch_raw(ref="nobody") is None


def test_the_raw_read_gives_one_shape_whichever_handle_was_used():
    """A path lookup answers with one object rather than an array. The operator
    surface reads rows, and an id having exactly one is not a different view."""
    _transport(_responds(json={"livemode": True, "subscription": SUBSCRIPTION}))

    raw = _fetch_raw(subscription_id="7d3b")

    assert raw is not None
    assert raw["subscriptions"] == [SUBSCRIPTION]
    assert raw["livemode"] is True


def test_the_raw_read_reports_an_id_flash_does_not_know_as_absence():
    """What the admin resolution flow reads: 404 here must mean "this id is
    wrong", not "try again later"."""
    _transport(_responds(status=404))

    assert _fetch_raw(subscription_id="nosuch") is None


def test_the_raw_read_never_reports_an_outage_as_absence():
    _transport(_responds(status=503))

    with pytest.raises(FlashUnavailable):
        _fetch_raw(ref="a" * 64)


def test_the_raw_read_does_not_retry_a_refused_credential():
    attempts = []
    _transport(_responds(status=401, calls=attempts))

    with pytest.raises(FlashCredentialError):
        _fetch_raw(ref="a" * 64)
    assert len(attempts) == 1


def test_the_raw_read_carries_no_credential_and_no_response_headers():
    from app.core.config import settings

    _transport(
        lambda request: httpx.Response(
            200,
            json={"livemode": True, "subscriptions": [SUBSCRIPTION]},
            headers={"x-flash-trace": "leak-me"},
        )
    )

    raw = _fetch_raw(ref="a" * 64)

    assert settings.flash_api_key not in str(raw)
    assert "leak-me" not in str(raw)


# ---------------------------------------------------------------------------
# Writing — cancelling, pausing, resuming
# ---------------------------------------------------------------------------
CANCELLED_AT_PERIOD_END = {
    **SUBSCRIPTION,
    # The trap this section exists for: Flash accepted the cancellation and
    # still reports the subscriber as active, because the account cancels at
    # period end. The date is the answer; the status is not.
    "status": "active",
    "canceledAt": "2026-08-31T15:06:09.067Z",
    "cancelEffectiveDate": "2026-09-20",
}


def _cancel(subscription_id="7d3b", **kwargs):
    return asyncio.run(flash.cancel_subscription(subscription_id, **kwargs))


def _set_status(subscription_id="7d3b", status="paused"):
    return asyncio.run(
        flash.set_subscription_status(subscription_id, status=status)
    )


def test_a_scheduled_cancellation_is_reported_by_its_date_not_its_status():
    """Flash answers 200 with the subscriber still active. Reading the status
    alone would report a failure on a cancellation that worked."""
    _transport(
        _responds(json={"livemode": True, "subscription": CANCELLED_AT_PERIOD_END})
    )

    cancelled = _cancel()

    assert cancelled is not None
    assert cancelled.status == "active"
    assert cancelled.cancel_effective_date == datetime(2026, 9, 20, 23, 59, 59, 999999)


def test_a_cancellation_is_posted_to_the_subscriptions_own_cancel_path():
    calls = []
    _transport(
        _responds(
            json={"livemode": True, "subscription": CANCELLED_AT_PERIOD_END},
            calls=calls,
        )
    )

    _cancel()

    assert calls[0].method == "POST"
    assert calls[0].url.path.endswith("/subscriptions/7d3b/cancel")


def test_a_cancellation_carries_the_reason_the_operator_gave():
    calls = []
    _transport(
        _responds(
            json={"livemode": True, "subscription": CANCELLED_AT_PERIOD_END},
            calls=calls,
        )
    )

    _cancel(reason="Refunded after a duplicate signup")

    assert json.loads(calls[0].content) == {
        "reason": "Refunded after a duplicate signup"
    }


def test_a_cancellation_with_no_reason_sends_none():
    """The field is optional, and an empty string is not a reason."""
    calls = []
    _transport(
        _responds(
            json={"livemode": True, "subscription": CANCELLED_AT_PERIOD_END},
            calls=calls,
        )
    )

    _cancel(reason="   ")

    assert json.loads(calls[0].content) == {}


def test_cancelling_something_flash_does_not_know_is_absence_not_a_failure():
    _transport(_responds(status=404))

    assert _cancel(subscription_id="nosuch") is None


def test_pausing_patches_the_status_flash_documents():
    calls = []
    _transport(
        _responds(
            json={
                "livemode": True,
                "subscription": {**SUBSCRIPTION, "status": "paused"},
            },
            calls=calls,
        )
    )

    paused = _set_status(status="paused")

    assert calls[0].method == "PATCH"
    assert calls[0].url.path.endswith("/subscriptions/7d3b")
    assert json.loads(calls[0].content) == {"status": "paused"}
    assert paused is not None and paused.status == "paused"


def test_resuming_puts_the_same_subscription_back_to_active():
    calls = []
    _transport(
        _responds(
            json={"livemode": True, "subscription": SUBSCRIPTION}, calls=calls
        )
    )

    resumed = _set_status(status="active")

    assert json.loads(calls[0].content) == {"status": "active"}
    assert resumed is not None and resumed.status == "active"


def test_a_status_flash_does_not_take_is_refused_before_anything_is_sent():
    calls = []
    _transport(_responds(json={}, calls=calls))

    with pytest.raises(FlashUnavailable):
        _set_status(status="canceled")
    assert not calls


def test_a_write_with_nothing_to_write_to_is_refused():
    with pytest.raises(FlashUnavailable):
        _cancel(subscription_id="")


def test_a_write_that_never_left_us_is_retried():
    """Connecting is the one failure that proves nothing was sent."""
    attempts = []

    def handler(request):
        attempts.append(request)
        if len(attempts) < 3:
            raise httpx.ConnectError("refused")
        return httpx.Response(
            200, json={"livemode": True, "subscription": CANCELLED_AT_PERIOD_END}
        )

    _transport(handler)

    assert _cancel() is not None
    assert len(attempts) == 3


def test_a_write_that_may_already_have_landed_is_never_repeated():
    """A read times out harmlessly; a cancellation may have gone through, and
    asking again would be a second write, not the same one."""
    attempts = []

    def handler(request):
        attempts.append(request)
        raise httpx.ReadTimeout("no answer")

    _transport(handler)

    with pytest.raises(FlashUnavailable):
        _cancel()
    assert len(attempts) == 1


def test_a_server_error_on_a_write_is_not_repeated_either():
    """Flash answered, so it received the request."""
    attempts = []
    _transport(_responds(status=500, calls=attempts))

    with pytest.raises(FlashUnavailable):
        _cancel()
    assert len(attempts) == 1


@pytest.mark.parametrize("status", [400, 409, 422])
def test_a_change_flash_declines_is_an_answer_not_an_outage(status):
    """Pausing something already cancelled is Flash saying no, not Flash being
    unreachable. Reported as an outage it would have an operator wait for a
    service that is up."""
    attempts = []
    _transport(_responds(status=status, calls=attempts))

    with pytest.raises(FlashRefused) as declined:
        _set_status(status="paused")
    assert declined.value.status_code == status
    assert len(attempts) == 1


def test_a_refusal_is_told_apart_from_an_outage_in_the_type_system():
    assert issubclass(FlashRefused, FlashUnavailable) is False


def test_a_read_flash_declines_is_still_our_url_being_wrong():
    """The asymmetry is deliberate: on the filtered list a 4xx says nothing
    about a subscriber and there is nothing an operator can act on."""
    _transport(_responds(status=400))

    with pytest.raises(FlashUnavailable):
        _fetch(ref="a" * 64)


@pytest.mark.parametrize("status", [401, 403])
def test_a_key_that_cannot_manage_subscriptions_is_named_as_such(status):
    """Writes need a scope reads do not, so this is the one failure a
    view-only key produces — and it is not Flash being down."""
    _transport(_responds(status=status))

    with pytest.raises(FlashCredentialError):
        _cancel()


def test_a_write_never_carries_the_api_key_into_its_error():
    from app.core.config import settings

    _transport(_responds(status=401))

    with pytest.raises(FlashCredentialError) as raised:
        _cancel()
    assert settings.flash_api_key not in str(raised.value)


def test_a_write_sends_the_key_as_a_bearer_token():
    from app.core.config import settings

    calls = []
    _transport(
        _responds(
            json={"livemode": True, "subscription": SUBSCRIPTION}, calls=calls
        )
    )

    _set_status(status="paused")

    assert calls[0].headers["Authorization"] == f"Bearer {settings.flash_api_key}"
