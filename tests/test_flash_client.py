"""Reading subscription state from Flash.

The distinction this file exists to protect: "Flash says they are not a
subscriber" is a fact we act on, while "we could not ask Flash" is not. Confusing
the two would revoke a paying user because a socket timed out.
"""

import asyncio
from datetime import datetime

import httpx
import pytest

from app.core import flash
from app.core.flash import (
    FlashCredentialError,
    FlashUnavailable,
    fetch_subscription,
    fetch_subscription_raw,
    parse_subscription,
)

SUBSCRIPTION = {
    "id": "7d3b",
    "status": "active",
    "ref": "a" * 64,
    "subscriberId": "a91c",
    "serviceId": "9c1e",
    "planId": "4f2a",
    "currentPeriodStart": "2026-08-20T14:03:11.000Z",
    "currentPeriodEnd": "2026-09-20T14:03:11.000Z",
    "nextBillingDate": "2026-09-20T14:03:11.000Z",
    "trialEndDate": None,
    "cancelEffectiveDate": None,
}


@pytest.fixture(autouse=True)
def reset_client():
    flash._client = None
    yield
    flash._client = None


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


def test_a_subscription_can_be_looked_up_by_flashs_identifier():
    calls = []
    _transport(_responds(json={"subscriptions": [SUBSCRIPTION]}, calls=calls))

    found = _fetch(subscription_id="7d3b")

    assert found is not None
    assert found.id == "7d3b"
    assert calls[0].url.params["subscriptionId"] == "7d3b"


def test_every_field_flash_sends_is_carried_across():
    found = parse_subscription(SUBSCRIPTION)

    assert found.status == "active"
    assert found.service_id == "9c1e"
    assert found.plan_id == "4f2a"
    assert found.current_period_end == datetime(2026, 9, 20, 14, 3, 11)
    assert found.cancel_effective_date is None


def test_a_status_we_do_not_recognise_survives_the_parse():
    found = parse_subscription({**SUBSCRIPTION, "status": "hibernating"})

    assert found.status == "hibernating"


# ---------------------------------------------------------------------------
# Not a subscriber, versus could not ask
# ---------------------------------------------------------------------------
def test_no_such_subscription_is_a_fact_not_a_failure():
    _transport(_responds(json={"subscriptions": []}))

    assert _fetch(ref="nobody") is None


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
    attempts = []
    _transport(_responds(status=status, calls=attempts))

    with pytest.raises(FlashUnavailable):
        _fetch(ref="a" * 64)
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
def test_asked_by_id_only_that_id_will_do():
    """Flash documents no ordering, and a re-subscribe leaves more than one row
    under one ref — taking the first would sometimes revoke a payer."""
    other = {**SUBSCRIPTION, "id": "other", "status": "expired"}
    _transport(_responds(json={"subscriptions": [other, SUBSCRIPTION]}))

    found = _fetch(subscription_id="7d3b")

    assert found is not None and found.id == "7d3b"


def test_asked_by_id_and_told_about_someone_else_is_not_a_match():
    _transport(_responds(json={"subscriptions": [{**SUBSCRIPTION, "id": "other"}]}))

    assert _fetch(subscription_id="7d3b") is None


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
    _transport(_responds(json={"subscriptions": [SUBSCRIPTION]}, calls=calls))

    _fetch_raw(ref="a" * 64)
    _fetch_raw(subscription_id="7d3b")

    assert calls[0].url.params["ref"] == "a" * 64
    assert calls[1].url.params["subscriptionId"] == "7d3b"


def test_the_raw_read_needs_a_handle_to_look_up_by():
    with pytest.raises(FlashUnavailable):
        _fetch_raw()


def test_the_raw_read_reports_no_such_subscription_as_absence():
    _transport(_responds(json={"livemode": True, "subscriptions": []}))

    assert _fetch_raw(ref="nobody") is None


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
