"""Inbound Flash subscription webhooks: signature, freshness, dedupe, recording.

Slice 01 establishes only that events arrive intact and are never lost — nothing
is interpreted, no user is resolved, no tier changes.

The DB is faked (``get_db`` yields a mock session) and the repo insert is patched
at the router namespace, so these assert HTTP behaviour and *whether a record was
attempted*, not persistence itself.
"""

import hashlib
import hmac
import json
import time
from datetime import datetime
from unittest.mock import AsyncMock

import pytest
from fastapi import APIRouter

from app.core.config import settings
from app.core.database import get_db
from app.services.flash_webhook_service import (
    MALFORMED_EVENT,
    FlashConfigError,
    build_dedupe_key,
    compute_signature,
    is_timestamp_fresh,
    parse_event_timestamp,
    parse_signature_header,
    signature_matches,
    validate_flash_config,
)

SECRET = "whsec_test_secret"


def _body(event: str = "subscription.activated", **data) -> bytes:
    payload = {
        "event": event,
        "timestamp": "2026-08-20T14:03:12.000Z",
        "data": {
            "accountId": "c410",
            "subscriptionId": "7d3b",
            "serviceId": "9c1e",
            "planId": "4f2a",
            "subscriberId": "a91c",
            "externalRef": "user_18342",
            "activatedAt": "2026-08-20T14:03:11.000Z",
            **data,
        },
    }
    return json.dumps(payload).encode()


def _sign(raw: bytes, timestamp: int | None = None, secret: str = SECRET) -> dict:
    ts = int(time.time()) if timestamp is None else timestamp
    mac = hmac.new(
        secret.encode(), f"{ts}.{raw.decode()}".encode(), hashlib.sha256
    ).hexdigest()
    return {"Flash-Signature": f"t={ts},v1={mac}", "Content-Type": "application/json"}


@pytest.fixture
def insert_event(monkeypatch) -> AsyncMock:
    """Patch the repo insert; returns True (inserted) unless a test says otherwise."""
    mock = AsyncMock(return_value=1)
    monkeypatch.setattr(
        "app.services.flash_webhook_service.insert_flash_webhook_event_on_db", mock
    )
    monkeypatch.setattr(
        "app.services.flash_webhook_service.mark_webhook_event_processed_on_db",
        AsyncMock(),
    )
    return mock


@pytest.fixture
def entitlement(monkeypatch) -> AsyncMock:
    """Stubbed — this file is about receiving and recording. What entitlement
    then does with the delivery is tests/test_flash_entitlement.py."""
    mock = AsyncMock()
    monkeypatch.setattr("app.services.flash_webhook_service.apply_entitlement", mock)
    return mock


@pytest.fixture
def webhook_client(client, monkeypatch, entitlement):
    from app.api import app

    async def _fake_get_db():
        yield AsyncMock()

    monkeypatch.setattr(settings, "flash_webhook_secret", SECRET)
    app.dependency_overrides[get_db] = _fake_get_db
    yield client


# ---------------------------------------------------------------------------
# Signature parsing and verification (pure)
# ---------------------------------------------------------------------------
def test_parses_a_well_formed_signature_header():
    parts = parse_signature_header("t=1700000000,v1=abc123")

    assert parts is not None
    assert parts.timestamp == 1700000000
    assert parts.signature == "abc123"


@pytest.mark.parametrize(
    "header",
    [
        None,
        "",
        "v1=abc123",
        "t=1700000000",
        "t=notanumber,v1=abc123",
        "garbage",
    ],
)
def test_rejects_malformed_signature_headers(header):
    assert parse_signature_header(header) is None


def test_signature_is_computed_over_timestamp_dot_raw_body():
    raw = b'{"event":"x"}'
    expected = hmac.new(
        SECRET.encode(), b"1700000000." + raw, hashlib.sha256
    ).hexdigest()

    assert compute_signature(SECRET, 1700000000, raw) == expected


def test_timestamp_freshness_window_is_symmetric():
    now = 1700000000

    assert is_timestamp_fresh(now, now=now, tolerance_seconds=300)
    assert is_timestamp_fresh(now - 299, now=now, tolerance_seconds=300)
    assert is_timestamp_fresh(now + 299, now=now, tolerance_seconds=300)
    assert not is_timestamp_fresh(now - 301, now=now, tolerance_seconds=300)
    assert not is_timestamp_fresh(now + 301, now=now, tolerance_seconds=300)


# ---------------------------------------------------------------------------
# Dedupe keys (pure)
# ---------------------------------------------------------------------------
def test_dedupe_key_uses_the_events_stable_discriminator():
    data = {"subscriptionId": "7d3b", "invoiceId": "inv_9"}

    key = build_dedupe_key("subscription.renewed", data, b"{}")

    assert key.startswith("7d3b:subscription.renewed:")
    assert "inv_9" in key


def test_dedupe_key_distinguishes_renewals_of_different_periods():
    first = build_dedupe_key(
        "subscription.renewed", {"subscriptionId": "s", "invoiceId": "inv_1"}, b"{}"
    )
    second = build_dedupe_key(
        "subscription.renewed", {"subscriptionId": "s", "invoiceId": "inv_2"}, b"{}"
    )

    assert first != second


def test_dedupe_key_distinguishes_event_types_for_one_subscription():
    data = {"subscriptionId": "s"}

    activated = build_dedupe_key("subscription.activated", data, b"a")
    expired = build_dedupe_key("subscription.expired", data, b"b")

    assert activated != expired


def test_unknown_events_still_dedupe_on_identical_bytes():
    raw = b'{"event":"subscription.mystery"}'

    first = build_dedupe_key("subscription.mystery", {"subscriptionId": "s"}, raw)
    second = build_dedupe_key("subscription.mystery", {"subscriptionId": "s"}, raw)

    assert first == second
    assert first != build_dedupe_key("subscription.mystery", {"subscriptionId": "s"}, b"other")


def test_known_event_missing_its_discriminator_falls_back_to_body_hash():
    """A malformed-but-signed delivery must still produce a usable key."""
    key = build_dedupe_key("subscription.renewed", {"subscriptionId": "s"}, b"raw")

    assert key.startswith("s:subscription.renewed:")
    assert "sha256=" in key


# ---------------------------------------------------------------------------
# Receiving deliveries
# ---------------------------------------------------------------------------
def test_valid_delivery_is_acknowledged_and_recorded(webhook_client, insert_event):
    raw = _body()

    response = webhook_client.post("/webhooks/flash", content=raw, headers=_sign(raw))

    assert response.status_code == 200
    insert_event.assert_awaited_once()
    recorded = insert_event.await_args.kwargs
    assert recorded["event"] == "subscription.activated"
    assert recorded["subscription_id"] == "7d3b"
    assert recorded["payload"]["data"]["externalRef"] == "user_18342"


def test_event_time_comes_from_the_body_not_the_delivery_attempt(
    webhook_client, insert_event
):
    """A retry of an old event carries a NEW header timestamp. Ordering on that
    would let a stale event win, so the body's own time is what we keep."""
    raw = _body()
    much_later = int(time.time())

    webhook_client.post(
        "/webhooks/flash", content=raw, headers=_sign(raw, timestamp=much_later)
    )

    recorded = insert_event.await_args.kwargs
    assert recorded["event_timestamp"] == datetime(2026, 8, 20, 14, 3, 12)
    assert recorded["delivery_timestamp"] == much_later


def test_event_time_is_absent_rather_than_wrong_when_flash_omits_it(
    webhook_client, insert_event
):
    raw = json.dumps({"event": "subscription.expired", "data": {}}).encode()

    webhook_client.post("/webhooks/flash", content=raw, headers=_sign(raw))

    assert insert_event.await_args.kwargs["event_timestamp"] is None


def test_recording_is_committed_before_acknowledging(webhook_client, insert_event):
    """The event must survive the process dying right after the 200."""
    session = AsyncMock()
    from app.api import app

    async def _fake_get_db():
        yield session

    app.dependency_overrides[get_db] = _fake_get_db
    raw = _body()

    response = webhook_client.post("/webhooks/flash", content=raw, headers=_sign(raw))

    assert response.status_code == 200
    session.commit.assert_awaited()


def test_tampered_body_is_refused_and_stores_nothing(webhook_client, insert_event):
    headers = _sign(_body())

    response = webhook_client.post(
        "/webhooks/flash", content=_body(subscriptionId="somebody_elses"), headers=headers
    )

    assert response.status_code == 401
    insert_event.assert_not_awaited()


def test_signature_from_the_wrong_secret_is_refused(webhook_client, insert_event):
    raw = _body()

    response = webhook_client.post(
        "/webhooks/flash", content=raw, headers=_sign(raw, secret="whsec_not_ours")
    )

    assert response.status_code == 401
    insert_event.assert_not_awaited()


def test_unsigned_delivery_is_refused(webhook_client, insert_event):
    response = webhook_client.post(
        "/webhooks/flash",
        content=_body(),
        headers={"Content-Type": "application/json"},
    )

    assert response.status_code == 400
    insert_event.assert_not_awaited()


def test_stale_delivery_is_refused_even_with_a_valid_signature(
    webhook_client, insert_event
):
    raw = _body()
    stale = int(time.time()) - (settings.flash_webhook_tolerance_seconds + 60)

    response = webhook_client.post(
        "/webhooks/flash", content=raw, headers=_sign(raw, timestamp=stale)
    )

    assert response.status_code == 400
    insert_event.assert_not_awaited()


def test_a_recorded_delivery_is_handed_to_entitlement(webhook_client, insert_event, entitlement):
    raw = _body()

    webhook_client.post("/webhooks/flash", content=raw, headers=_sign(raw))

    entitlement.assert_awaited_once()
    assert entitlement.await_args.kwargs["external_ref"] == "user_18342"
    assert entitlement.await_args.kwargs["subscription_id"] == "7d3b"


def test_a_retry_is_acknowledged_without_redoing_the_work(
    webhook_client, insert_event, entitlement
):
    """The original row records whether it was applied, and the recovery sweep
    retries it if not — so a redelivery has nothing to do but say 200."""
    insert_event.return_value = None  # already recorded
    raw = _body()

    response = webhook_client.post("/webhooks/flash", content=raw, headers=_sign(raw))

    assert response.status_code == 200
    entitlement.assert_not_awaited()


def test_a_failing_entitlement_still_acknowledges_the_delivery(
    webhook_client, insert_event, entitlement
):
    """The event is already durable. A non-2xx would only make Flash redeliver
    something we hold — and it gives up after a few tries."""
    entitlement.side_effect = RuntimeError("neo4j is on fire")
    raw = _body()

    response = webhook_client.post("/webhooks/flash", content=raw, headers=_sign(raw))

    assert response.status_code == 200


def test_retried_delivery_is_acknowledged_without_duplicating(
    webhook_client, insert_event
):
    insert_event.return_value = None  # unique violation → nothing inserted
    raw = _body()

    response = webhook_client.post("/webhooks/flash", content=raw, headers=_sign(raw))

    assert response.status_code == 200
    assert response.json()["duplicate"] is True


def test_unrecognised_event_type_is_recorded_and_acknowledged(
    webhook_client, insert_event
):
    raw = _body(event="subscription.something_new")

    response = webhook_client.post("/webhooks/flash", content=raw, headers=_sign(raw))

    assert response.status_code == 200
    assert insert_event.await_args.kwargs["event"] == "subscription.something_new"


def test_signed_but_unparseable_body_is_still_recorded(webhook_client, insert_event):
    """Authenticated means Flash sent it. Flash never replays, so never drop it."""
    raw = b"not json at all"

    response = webhook_client.post("/webhooks/flash", content=raw, headers=_sign(raw))

    assert response.status_code == 200
    recorded = insert_event.await_args.kwargs
    assert recorded["event"] == MALFORMED_EVENT
    assert "not json at all" in recorded["payload"]["_unparseable"]


def test_signed_body_without_an_event_name_is_still_recorded(
    webhook_client, insert_event
):
    raw = json.dumps({"data": {"subscriptionId": "s"}}).encode()

    response = webhook_client.post("/webhooks/flash", content=raw, headers=_sign(raw))

    assert response.status_code == 200
    assert insert_event.await_args.kwargs["event"] == MALFORMED_EVENT


def test_non_ascii_signature_is_refused_rather_than_raising():
    """`compare_digest` rejects non-ASCII str with a TypeError. Starlette decodes
    header bytes as latin-1, so a raw socket can put non-ASCII in `v1` — on an
    unauthenticated public route that would be an unhandled 500."""
    assert signature_matches(SECRET, 1700000000, b"{}", "\u00e9\u00e9\u00e9") is False


def test_the_emitter_script_signs_exactly_as_the_server_verifies():
    """`scripts/emit_flash_webhook` is stdlib-only so it runs without an `.env`,
    which means it repeats the HMAC. This is what stops the two drifting."""
    from scripts.emit_flash_webhook import sign

    raw = _body()

    assert sign(SECRET, 1700000000, raw) == compute_signature(SECRET, 1700000000, raw)
    assert signature_matches(SECRET, 1700000000, raw, sign(SECRET, 1700000000, raw))


def test_event_timestamp_converts_an_offset_instead_of_stripping_it():
    """Flash sends Z today; a future non-zero offset must convert to UTC, not
    be silently dropped — dropped, the naive value is wrong by the offset."""
    assert parse_event_timestamp(
        {"timestamp": "2026-08-20T16:03:12.000+02:00"}
    ) == datetime(2026, 8, 20, 14, 3, 12)


def test_event_timestamp_parses_flashs_iso_format():
    assert parse_event_timestamp({"timestamp": "2026-08-20T14:03:12.000Z"}) == datetime(
        2026, 8, 20, 14, 3, 12
    )


@pytest.mark.parametrize("payload", [{}, {"timestamp": None}, {"timestamp": "later"}])
def test_event_timestamp_is_absent_rather_than_guessed(payload):
    assert parse_event_timestamp(payload) is None


def test_no_response_ever_echoes_the_signing_secret(webhook_client, insert_event):
    raw = _body()

    for headers in (_sign(raw, secret="wrong"), {"Content-Type": "application/json"}):
        response = webhook_client.post("/webhooks/flash", content=raw, headers=headers)
        assert SECRET not in response.text


# ---------------------------------------------------------------------------
# Mounting and configuration
# ---------------------------------------------------------------------------
def test_webhook_route_is_absent_when_payments_are_not_configured(monkeypatch):
    from app.routers.router import include_billing_routers

    monkeypatch.setattr(settings, "flash_enabled", False)
    bare = APIRouter()

    include_billing_routers(bare)

    assert bare.routes == []


def test_webhook_route_is_mounted_when_payments_are_configured(monkeypatch):
    from app.routers.router import include_billing_routers

    monkeypatch.setattr(settings, "flash_enabled", True)
    bare = APIRouter()

    include_billing_routers(bare)

    paths = [route.path for route in bare.routes]
    assert "/webhooks/flash" in paths
    # The operator surface is mounted by the same switch: a deployment with no
    # Flash account has no billing to look at.
    assert any(path.startswith("/admin/billing") for path in paths)


def test_enabling_payments_without_credentials_fails_fast():
    with pytest.raises(FlashConfigError) as excinfo:
        validate_flash_config(enabled=True, api_key="", webhook_secret="")

    message = str(excinfo.value)
    assert "flash_api_key" in message
    assert "flash_webhook_secret" in message


def test_startup_names_only_the_missing_credential():
    with pytest.raises(FlashConfigError) as excinfo:
        validate_flash_config(enabled=True, api_key="sk_live_x", webhook_secret="")

    message = str(excinfo.value)
    assert "flash_webhook_secret" in message
    assert "flash_api_key" not in message


def test_disabled_payments_need_no_credentials():
    validate_flash_config(enabled=False, api_key="", webhook_secret="")


def test_config_error_never_contains_the_secret_value():
    with pytest.raises(FlashConfigError) as excinfo:
        validate_flash_config(enabled=True, api_key="sk_live_abc", webhook_secret="")

    assert "sk_live_abc" not in str(excinfo.value)


def test_a_delivery_we_finished_is_marked_so_replay_skips_it(
    webhook_client, insert_event, entitlement, monkeypatch
):
    from unittest.mock import AsyncMock as _AsyncMock
    from app.services.billing_service import EntitlementReason
    from types import SimpleNamespace

    entitlement.return_value = SimpleNamespace(
        applied=True, reason=EntitlementReason.GRANTED
    )
    marked = _AsyncMock()
    monkeypatch.setattr(
        "app.services.flash_webhook_service.mark_webhook_event_processed_on_db", marked
    )
    raw = _body()

    webhook_client.post("/webhooks/flash", content=raw, headers=_sign(raw))

    marked.assert_awaited_once()
    assert marked.await_args.args[1] == 1


def test_a_delivery_that_settled_nothing_is_surfaced_not_marked_done(
    webhook_client, insert_event, entitlement, monkeypatch
):
    """An event naming nobody we know would otherwise be marked processed and
    disappear — the divergence report is where an operator finds it."""
    from unittest.mock import AsyncMock as _AsyncMock
    from app.services.billing_service import EntitlementReason
    from types import SimpleNamespace

    entitlement.return_value = SimpleNamespace(
        applied=False, reason=EntitlementReason.UNKNOWN_USER
    )
    marked, failed = _AsyncMock(), _AsyncMock()
    monkeypatch.setattr(
        "app.services.flash_webhook_service.mark_webhook_event_processed_on_db", marked
    )
    monkeypatch.setattr(
        "app.services.flash_webhook_service.record_webhook_event_failure_on_db", failed
    )
    raw = _body()

    webhook_client.post("/webhooks/flash", content=raw, headers=_sign(raw))

    marked.assert_not_awaited()
    failed.assert_awaited_once()
    assert failed.await_args.args[2] == "unknown_user"


def test_a_delivery_that_failed_is_left_for_replay(
    webhook_client, insert_event, entitlement, monkeypatch
):
    """Not marking it is what brings it back — that is the whole recovery path."""
    from unittest.mock import AsyncMock as _AsyncMock

    marked = _AsyncMock()
    monkeypatch.setattr(
        "app.services.flash_webhook_service.mark_webhook_event_processed_on_db", marked
    )
    entitlement.side_effect = RuntimeError("died mid-write")
    raw = _body()

    response = webhook_client.post("/webhooks/flash", content=raw, headers=_sign(raw))

    assert response.status_code == 200
    marked.assert_not_awaited()
