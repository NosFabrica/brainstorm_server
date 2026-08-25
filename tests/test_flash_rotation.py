"""Replacing the Flash credentials without dropping a delivery.

Flash signs with whatever secret is current at the moment it sends. During a
rotation both are legitimately in flight, and Flash retries a rejected delivery
only a few times before never sending it again — so a flag-day swap loses real
events.
"""

import hashlib
import hmac
import json
import time

import pytest

from app.core.config import settings
from app.services.flash_webhook_service import (
    FlashSignatureError,
    accepted_webhook_secrets,
    verify_delivery,
)

CURRENT = "whsec_current"
PREVIOUS = "whsec_previous"
BODY = json.dumps({"event": "subscription.activated", "data": {}}).encode()


def _header(secret: str, timestamp: int | None = None) -> str:
    ts = timestamp or int(time.time())
    mac = hmac.new(
        secret.encode(), f"{ts}.".encode() + BODY, hashlib.sha256
    ).hexdigest()
    return f"t={ts},v1={mac}"


def _verify(header: str):
    return verify_delivery(
        signature_header=header,
        raw_body=BODY,
        secrets=accepted_webhook_secrets(),
        now=int(time.time()),
        tolerance_seconds=300,
    )


@pytest.fixture(autouse=True)
def rotating(monkeypatch):
    monkeypatch.setattr(settings, "flash_webhook_secret", CURRENT)
    monkeypatch.setattr(settings, "flash_webhook_secret_previous", PREVIOUS)


# ---------------------------------------------------------------------------
# The overlap window
# ---------------------------------------------------------------------------
def test_a_delivery_signed_with_the_new_secret_is_accepted():
    assert _verify(_header(CURRENT)) is not None


def test_a_delivery_signed_with_the_old_secret_is_still_accepted():
    """Flash may already have signed and queued it before we swapped."""
    assert _verify(_header(PREVIOUS)) is not None


def test_a_delivery_signed_with_neither_is_refused():
    with pytest.raises(FlashSignatureError):
        _verify(_header("whsec_someone_elses"))


def test_the_old_secret_stops_being_accepted_once_it_is_removed(monkeypatch):
    """Which is what ends the window — leaving it set forever would mean a
    compromised secret stays valid indefinitely."""
    monkeypatch.setattr(settings, "flash_webhook_secret_previous", "")

    with pytest.raises(FlashSignatureError):
        _verify(_header(PREVIOUS))


def test_only_the_current_secret_is_accepted_when_no_rotation_is_underway(monkeypatch):
    monkeypatch.setattr(settings, "flash_webhook_secret_previous", "")

    assert accepted_webhook_secrets() == (CURRENT,)


def test_the_current_secret_is_tried_first():
    """The overlap is temporary and most traffic is signed with the new one."""
    assert accepted_webhook_secrets()[0] == CURRENT


def test_an_empty_previous_secret_never_becomes_an_accepted_one(monkeypatch):
    """It must be absent from the list, not present and matching nothing."""
    monkeypatch.setattr(settings, "flash_webhook_secret_previous", "")

    assert "" not in accepted_webhook_secrets()


def test_a_stale_delivery_is_still_refused_during_a_rotation():
    """The overlap widens which secrets are accepted, not the replay window."""
    stale = int(time.time()) - 3600

    with pytest.raises(FlashSignatureError):
        _verify(_header(CURRENT, timestamp=stale))


# ---------------------------------------------------------------------------
# Not leaking either credential
# ---------------------------------------------------------------------------
def test_a_rejected_delivery_never_names_a_secret():
    try:
        _verify(_header("whsec_someone_elses"))
    except FlashSignatureError as refused:
        assert CURRENT not in str(refused)
        assert PREVIOUS not in str(refused)


def test_the_committed_example_env_carries_no_secret_values():
    """env.example ships to everyone; a real secret pasted there is published."""
    from pathlib import Path

    example = Path(__file__).resolve().parent.parent / "env.example"
    for line in example.read_text().splitlines():
        if line.startswith(("FLASH_API_KEY=", "FLASH_WEBHOOK_SECRET")):
            assert line.split("=", 1)[1] == "", f"{line} carries a value"


# ---------------------------------------------------------------------------
# The check itself
# ---------------------------------------------------------------------------
def test_a_credential_check_is_recorded_but_never_interpreted(monkeypatch):
    """It must prove the receiving path works without changing anyone's tier —
    and without landing in the divergence report as an unmatchable event."""
    import asyncio
    from unittest.mock import AsyncMock

    from app.services import flash_webhook_service as svc

    entitlement, marked, failed = AsyncMock(), AsyncMock(), AsyncMock()
    monkeypatch.setattr(svc, "apply_entitlement", entitlement)
    monkeypatch.setattr(svc, "mark_webhook_event_processed_on_db", marked)
    monkeypatch.setattr(svc, "record_webhook_event_failure_on_db", failed)
    monkeypatch.setattr(
        svc, "insert_flash_webhook_event_on_db", AsyncMock(return_value=1)
    )

    body = json.dumps({"event": svc.PROBE_EVENT, "data": {}}).encode()
    asyncio.run(svc.handle_delivery(AsyncMock(), raw_body=body, delivery_timestamp=1))

    entitlement.assert_not_awaited()
    marked.assert_awaited_once()
    failed.assert_not_awaited()


# ---------------------------------------------------------------------------
# Staying visible while a credential is broken
# ---------------------------------------------------------------------------
def test_a_credential_failure_mid_delivery_is_recorded_not_just_logged(monkeypatch):
    """During the compromised-key path the API key is dead for a few minutes and
    every delivery's entitlement raises. Those rows must carry a reason: the
    divergence report keys on process_error, so a null one is invisible to the
    operator watching the incident until the abandoned sweep runs."""
    import asyncio
    from unittest.mock import AsyncMock

    from app.core.flash import FlashCredentialError
    from app.services import flash_webhook_service as svc

    failed = AsyncMock()
    monkeypatch.setattr(svc, "record_webhook_event_failure_on_db", failed)
    monkeypatch.setattr(svc, "mark_webhook_event_processed_on_db", AsyncMock())
    monkeypatch.setattr(
        svc, "apply_entitlement", AsyncMock(side_effect=FlashCredentialError("401"))
    )
    monkeypatch.setattr(
        svc, "insert_flash_webhook_event_on_db", AsyncMock(return_value=7)
    )

    db = AsyncMock()
    asyncio.run(svc.handle_delivery(db, raw_body=BODY, delivery_timestamp=1))

    failed.assert_awaited_once()
    assert failed.await_args.args[1] == 7
    # Left unprocessed, so the replay sweep still owns recovering it.
    assert "FlashCredentialError" in failed.await_args.args[2]


def test_a_poisoned_session_still_records_the_reason(monkeypatch):
    """A DB error aborts the transaction, so recording the reason on the same
    session would raise too. Roll back first or the reason is lost exactly when
    something is badly wrong."""
    import asyncio
    from unittest.mock import AsyncMock

    from app.services import flash_webhook_service as svc

    calls = []
    db = AsyncMock()
    db.rollback = AsyncMock(side_effect=lambda: calls.append("rollback"))
    monkeypatch.setattr(
        svc,
        "record_webhook_event_failure_on_db",
        AsyncMock(side_effect=lambda *a, **k: calls.append("record")),
    )
    monkeypatch.setattr(svc, "mark_webhook_event_processed_on_db", AsyncMock())
    monkeypatch.setattr(
        svc, "apply_entitlement", AsyncMock(side_effect=RuntimeError("connection lost"))
    )
    monkeypatch.setattr(
        svc, "insert_flash_webhook_event_on_db", AsyncMock(return_value=7)
    )

    asyncio.run(svc.handle_delivery(db, raw_body=BODY, delivery_timestamp=1))

    assert calls == ["rollback", "record"]


def test_a_delivery_survives_even_if_recording_the_reason_also_fails(monkeypatch):
    """The reason is a nicety; the acknowledgement is not. Flash drops an event
    it cannot deliver, so nothing in the failure path may propagate."""
    import asyncio
    from unittest.mock import AsyncMock

    from app.services import flash_webhook_service as svc

    monkeypatch.setattr(
        svc,
        "record_webhook_event_failure_on_db",
        AsyncMock(side_effect=RuntimeError("still broken")),
    )
    monkeypatch.setattr(svc, "mark_webhook_event_processed_on_db", AsyncMock())
    monkeypatch.setattr(
        svc, "apply_entitlement", AsyncMock(side_effect=RuntimeError("boom"))
    )
    monkeypatch.setattr(
        svc, "insert_flash_webhook_event_on_db", AsyncMock(return_value=7)
    )

    recorded = asyncio.run(
        svc.handle_delivery(AsyncMock(), raw_body=BODY, delivery_timestamp=1)
    )
    assert recorded.event_id == 7


# ---------------------------------------------------------------------------
# Knowing the window is still open
# ---------------------------------------------------------------------------
def test_a_rotation_left_half_finished_is_announced_at_boot():
    """The overlap has no expiry. Its only other signal is an INFO line that
    appears when a delivery arrives on the old secret — and whose absence is
    indistinguishable from 'rotation finished'. Say so at every boot instead."""
    from app.services.flash_webhook_service import describe_rotation_state

    assert describe_rotation_state(previous_secret=PREVIOUS) is not None
    assert describe_rotation_state(previous_secret="") is None
    assert PREVIOUS not in (describe_rotation_state(previous_secret=PREVIOUS) or "")


# ---------------------------------------------------------------------------
# The checker script
# ---------------------------------------------------------------------------
def test_the_checker_refuses_to_pass_having_checked_nothing():
    """Skipping both checks used to print 'Rotation verified.' and exit 0 — the
    exact reassurance that would let someone delete a working secret."""
    import subprocess
    import sys
    from pathlib import Path

    root = Path(__file__).resolve().parent.parent
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "scripts.check_flash_credentials",
            "--skip-api",
            "--skip-webhook",
        ],
        cwd=root,
        capture_output=True,
        text=True,
    )
    assert result.returncode != 0
    assert "Rotation verified" not in result.stdout


def test_the_checker_signs_the_way_the_server_verifies():
    """Third copy of the HMAC. It is stdlib-only on purpose — it must run
    against a deployed host with no .env — so it cannot import the real one,
    which makes silent drift the failure mode this test exists to catch."""
    from scripts.check_flash_credentials import sign
    from app.services.flash_webhook_service import signature_matches

    timestamp = 1700000000
    assert signature_matches(CURRENT, timestamp, BODY, sign(CURRENT, timestamp, BODY))


def test_the_checker_takes_credentials_from_the_environment_only():
    """argv is world-readable in `ps` and lands in shell history."""
    from pathlib import Path

    source = (
        Path(__file__).resolve().parent.parent
        / "scripts"
        / "check_flash_credentials.py"
    ).read_text()
    assert "--api-key" not in source
    assert "--secret" not in source
