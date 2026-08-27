"""Receiving inbound Flash subscription webhooks.

Flash signs each delivery with an HMAC over ``{timestamp}.{raw body}``, sent as
``Flash-Signature: t=<unix>,v1=<hex>``. Signatures are checked against the exact
bytes received — re-serializing the JSON would change them and every delivery
would fail.

Once the signature passes we know Flash sent it, so from that point the rule is
**record everything, reject nothing**: Flash retries a failed delivery only a few
times and then never sends it again.
"""

import asyncio
import hashlib
import hmac
import json
from dataclasses import dataclass
from datetime import datetime

from sqlalchemy.exc import InterfaceError, OperationalError
from sqlalchemy.ext.asyncio import AsyncSession as AsyncDBSession

from app.core.config import settings
from app.core.flash import FlashUnavailable, parse_flash_timestamp
from app.core.loggr import loggr
from app.repos.flash_webhook_event_repo import (
    insert_flash_webhook_event_on_db,
    mark_webhook_event_processed_on_db,
    record_webhook_event_failure_on_db,
)
from app.repos.user_subscription_repo import update_last_event_at_on_db
from app.services.billing_service import (
    SETTLED_REASONS,
    apply_entitlement,
    apply_payload_fallback,
    utc_now,
)

logger = loggr.get_logger(__name__)

SIGNATURE_HEADER = "Flash-Signature"

# Recorded in place of the event name when a signed body doesn't carry one.
MALFORMED_EVENT = "_malformed"

# What `scripts/check_flash_credentials` sends. Recorded like anything else —
# that is the point, it proves the receiving path works — but never interpreted,
# so a rotation check cannot change a tier or turn up in the divergence report
# as an event nobody could match.
PROBE_EVENT = "credential.check"

# Fields that make a delivery of a given event unique. First match wins.
_DISCRIMINATORS: dict[str, tuple[str, ...]] = {
    "subscription.activated": ("activatedAt",),
    "subscription.renewed": ("invoiceId", "periodNumber"),
    "subscription.past_due": ("attemptNumber", "firstFailedAt"),
    "subscription.canceled": ("canceledAt",),
    "subscription.expired": ("expiredAt",),
}


class FlashConfigError(RuntimeError):
    """Payments are enabled but unusable. Raised at startup, never per-request."""


class FlashSignatureError(Exception):
    """The delivery did not come from Flash, or came too long ago."""

    def __init__(self, reason: str, status_code: int) -> None:
        super().__init__(reason)
        self.reason = reason
        self.status_code = status_code


@dataclass(frozen=True)
class SignatureParts:
    timestamp: int
    signature: str


@dataclass(frozen=True)
class DeliveryTarget:
    """Who an event is about. One decoder, so the live path and the replay path
    cannot come to different conclusions about the same payload."""

    external_ref: str | None
    subscription_id: str | None


def delivery_target(payload: dict | None) -> DeliveryTarget:
    data = (payload or {}).get("data")
    if not isinstance(data, dict):
        data = {}
    return DeliveryTarget(
        external_ref=data.get("externalRef"),
        subscription_id=data.get("subscriptionId"),
    )


@dataclass(frozen=True)
class RecordedDelivery:
    event_id: int | None
    event: str
    subscription_id: str | None
    external_ref: str | None
    duplicate: bool
    event_timestamp: datetime | None = None

    @property
    def needs_processing(self) -> bool:
        """Whether anything should happen after the ack. Duplicates are already
        owned by their original row; probes are settled on receipt."""
        return (
            not self.duplicate
            and self.event_id is not None
            and self.event != PROBE_EVENT
        )


def parse_signature_header(raw: str | None) -> SignatureParts | None:
    """Parse ``t=<unix seconds>,v1=<hex>``. None if absent or malformed."""
    if not raw:
        return None
    fields: dict[str, str] = {}
    for segment in raw.split(","):
        key, sep, value = segment.partition("=")
        if sep:
            fields[key.strip()] = value.strip()
    timestamp, signature = fields.get("t"), fields.get("v1")
    if not timestamp or not signature:
        return None
    try:
        return SignatureParts(timestamp=int(timestamp), signature=signature)
    except ValueError:
        return None


def is_timestamp_fresh(timestamp: int, now: int, tolerance_seconds: int) -> bool:
    """Reject replays. Symmetric, so clock skew either way is tolerated equally."""
    return abs(now - timestamp) <= tolerance_seconds


def compute_signature(secret: str, timestamp: int, raw_body: bytes) -> str:
    signed = str(timestamp).encode() + b"." + raw_body
    return hmac.new(secret.encode(), signed, hashlib.sha256).hexdigest()


def signature_matches(
    secret: str, timestamp: int, raw_body: bytes, provided: str
) -> bool:
    # Compared as bytes: compare_digest rejects non-ASCII str with a TypeError,
    # and `provided` is attacker-controlled on a public endpoint.
    expected = compute_signature(secret, timestamp, raw_body).encode()
    return hmac.compare_digest(expected, provided.encode())


def build_dedupe_key(event: str, data: dict, raw_body: bytes) -> str:
    """A stable identity for one delivery, so Flash's retries collapse to one row.

    Keyed on subscription + event + whichever field distinguishes this
    occurrence. An unrecognised event, or one missing its discriminator, falls
    back to hashing the exact bytes — weaker, but never absent, which is what
    matters for an at-least-once sender.
    """
    subscription_id = str(data.get("subscriptionId") or "")
    for field in _DISCRIMINATORS.get(event, ()):
        value = data.get(field)
        if value is not None:
            return f"{subscription_id}:{event}:{field}={value}"
    digest = hashlib.sha256(raw_body).hexdigest()
    return f"{subscription_id}:{event}:sha256={digest}"


def parse_event_timestamp(payload: dict) -> datetime | None:
    """The moment the event *happened*, per Flash's body.

    Deliberately not the signature's ``t``, which is the delivery *attempt* —
    a retry of an old event carries a newer one, so ordering on it would let a
    stale event win.
    """
    return parse_flash_timestamp(payload.get("timestamp"))


def accepted_webhook_secrets() -> tuple[str, ...]:
    """Which signing secrets are currently valid, newest first.

    More than one only during a rotation: Flash signs with whatever is current
    when it sends, and retries a rejected delivery only a few times before
    dropping it forever, so a flag-day swap loses whatever was already in
    flight. Clearing the previous value and rolling the server ends the window.
    """
    return tuple(
        secret
        for secret in (
            settings.flash_webhook_secret,
            settings.flash_webhook_secret_previous,
        )
        if secret
    )


def verify_delivery(
    *,
    signature_header: str | None,
    raw_body: bytes,
    secrets: tuple[str, ...],
    now: int,
    tolerance_seconds: int,
) -> SignatureParts:
    """Prove the delivery came from Flash. Raises FlashSignatureError otherwise."""
    parts = parse_signature_header(signature_header)
    if parts is None:
        raise FlashSignatureError("malformed_signature_header", 400)
    if not is_timestamp_fresh(parts.timestamp, now, tolerance_seconds):
        raise FlashSignatureError("stale_timestamp", 400)

    for index, secret in enumerate(secrets):
        if signature_matches(secret, parts.timestamp, raw_body, parts.signature):
            if index > 0:
                # The only signal that Flash has not caught up, so the old
                # secret cannot be removed yet.
                logger.info(
                    "Flash delivery accepted on a superseded signing secret; "
                    "a rotation is still in flight"
                )
            return parts

    raise FlashSignatureError("invalid_signature", 401)


def _read_body(raw_body: bytes) -> tuple[str, dict, dict]:
    """(event name, data, payload to store) for an already-authenticated body.

    Never raises. A body we can't read still came from Flash, so it is recorded
    under a sentinel event rather than dropped — losing it would mean losing the
    only evidence of whatever produced it.
    """
    try:
        payload = json.loads(raw_body)
    except (ValueError, UnicodeDecodeError):
        logger.error("Flash sent a signed body that is not JSON; recording verbatim")
        return MALFORMED_EVENT, {}, {"_unparseable": raw_body.decode(errors="replace")}

    if not isinstance(payload, dict):
        logger.error("Flash sent a signed body that is not an object; recording it")
        return MALFORMED_EVENT, {}, {"_unexpected": payload}

    event = payload.get("event")
    if not isinstance(event, str) or not event:
        logger.error("Flash sent a signed event with no name; recording it")
        event = MALFORMED_EVENT

    data = payload.get("data")
    return event, data if isinstance(data, dict) else {}, payload


async def record_delivery(
    db: AsyncDBSession, *, raw_body: bytes, delivery_timestamp: int
) -> RecordedDelivery:
    """Persist one authenticated delivery and commit, before anything acks it.

    The commit is explicit so "committed before acknowledged" is a property of
    this function rather than of FastAPI's dependency-teardown ordering.
    """
    event, data, payload = _read_body(raw_body)
    target = delivery_target(payload)
    subscription_id, external_ref = target.subscription_id, target.external_ref

    event_id = await insert_flash_webhook_event_on_db(
        db,
        event=event,
        delivery_timestamp=delivery_timestamp,
        event_timestamp=parse_event_timestamp(payload),
        subscription_id=subscription_id,
        payload=payload,
        dedupe_key=build_dedupe_key(event, data, raw_body),
        # We are the worker for this event, from this moment.
        claimed_at=utc_now(),
    )
    await db.commit()
    inserted = event_id is not None

    logger.info(
        "Flash webhook %s recorded (subscription=%s, duplicate=%s)",
        event,
        subscription_id,
        not inserted,
    )
    return RecordedDelivery(
        event_id=event_id,
        event=event,
        subscription_id=subscription_id,
        external_ref=external_ref,
        duplicate=not inserted,
        event_timestamp=parse_event_timestamp(payload),
    )


async def handle_delivery(
    db: AsyncDBSession, *, raw_body: bytes, delivery_timestamp: int
) -> RecordedDelivery:
    """Record an authenticated delivery. Entitlement happens after the ack.

    Flash allows ten seconds to answer and retries only three times, so nothing
    slower than a local insert belongs before the 200. The caller schedules
    `process_delivery` once the response is on its way; if the process dies
    before that runs, the recovery sweep replays the row.
    """
    recorded = await record_delivery(
        db, raw_body=raw_body, delivery_timestamp=delivery_timestamp
    )

    if recorded.event == PROBE_EVENT:
        if recorded.event_id is not None:
            await mark_webhook_event_processed_on_db(
                db, recorded.event_id, now=utc_now()
            )
            await db.commit()
        logger.info("Flash credential check accepted")

    return recorded


async def process_delivery_in_background(recorded: RecordedDelivery) -> None:
    """Entry point for the post-ack half, run as a FastAPI background task.

    Opens its own session — the request's is gone by the time this runs — and
    never raises: the event is recorded, so any failure here is recoverable by
    the replay sweep.
    """
    from app.core.database import db_session

    try:
        async with db_session() as db:
            await process_delivery(db, recorded)
    except Exception:
        logger.exception(
            "Processing Flash event %s failed; it is recorded and replayable",
            recorded.event_id,
        )


async def process_delivery(db: AsyncDBSession, recorded: RecordedDelivery) -> None:
    """Reconcile the subscriber a recorded delivery names.

    Failure never surfaces to Flash — the event is already durable, and a
    non-2xx was never sent, so there is nothing to signal. An unreachable Flash
    additionally applies what the payload unambiguously implies (see
    `apply_payload_fallback`), leaving the event unprocessed so the cron
    re-reads the authoritative state later.
    """
    if not recorded.needs_processing:
        return

    try:
        outcome = await _apply_with_db_retries(
            db,
            external_ref=recorded.external_ref,
            subscription_id=recorded.subscription_id,
        )
        if recorded.event_id is not None:
            if outcome.reason in SETTLED_REASONS:
                # Marked done so replay doesn't redo work that succeeded.
                await mark_webhook_event_processed_on_db(
                    db, recorded.event_id, now=utc_now()
                )
                await update_last_event_at_on_db(
                    db,
                    pubkey=recorded.external_ref,
                    event_timestamp=recorded.event_timestamp,
                )
            else:
                # Nothing was decided — the event names nobody we know, or a
                # plan we don't map. Left unprocessed with a reason so it
                # surfaces to an operator instead of disappearing.
                await record_webhook_event_failure_on_db(
                    db, recorded.event_id, outcome.reason.value
                )
            await db.commit()
    except FlashUnavailable as err:
        # Flash cannot be asked, so the convergent path is closed — but some
        # payloads carry their own answer. The event stays unprocessed either
        # way, so the cron still re-reads the authoritative state.
        logger.warning(
            "Flash unreachable while processing event %s; trying the payload "
            "fallback",
            recorded.event_id,
        )
        await _record_failure(db, recorded.event_id, err)
        await apply_payload_fallback(
            db, event=recorded.event, external_ref=recorded.external_ref
        )
    except Exception as err:
        logger.exception(
            "Entitlement failed for %s; the event is recorded and replayable",
            recorded.external_ref,
        )
        await _record_failure(db, recorded.event_id, err)


# In-process attempts for a transient database failure — a deadlock, a
# serialization abort, a connection blip. Safe to retry whole because the write
# is convergent; anything past the cap is the cron's job.
_DB_RETRIES = 3


async def _apply_with_db_retries(
    db: AsyncDBSession, *, external_ref: str | None, subscription_id: str | None
):
    for attempt in range(1, _DB_RETRIES + 1):
        try:
            return await apply_entitlement(
                db, external_ref=external_ref, subscription_id=subscription_id
            )
        except (OperationalError, InterfaceError):
            if attempt == _DB_RETRIES:
                raise
            await db.rollback()
            await asyncio.sleep(0.1 * attempt)
    raise AssertionError("unreachable")


async def _record_failure(
    db: AsyncDBSession, event_id: int | None, err: BaseException
) -> None:
    """Give the row a reason so it shows up in the divergence report.

    Without one it is invisible there until the abandoned sweep runs — the whole
    window of a broken credential would look quiet. Rolls back first because a
    database error leaves the session unusable, and swallows its own failure:
    the reason is a nicety, acknowledging the delivery is not.
    """
    if event_id is None:
        return
    try:
        await db.rollback()
        await record_webhook_event_failure_on_db(
            db, event_id, f"{type(err).__name__}: {err}"[:500]
        )
        await db.commit()
    except Exception:
        logger.exception(
            "Could not record why entitlement failed for event %s", event_id
        )


def describe_rotation_state(previous_secret: str) -> str | None:
    """What to say at boot when a rotation was started and never finished.

    The overlap has no expiry, and its only other signal is an INFO line whose
    absence means either "Flash caught up" or "nobody cleared it". Never names
    the value.
    """
    if not previous_secret:
        return None
    return (
        "FLASH_WEBHOOK_SECRET_PREVIOUS is set: deliveries signed with the "
        "superseded secret are still accepted. Clear it once Flash has caught "
        "up — see docs/flash/credential-rotation.md."
    )


def validate_flash_config(
    enabled: bool, api_key: str, webhook_secret: str, base_url: str
) -> None:
    """Refuse to start half-configured.

    Booting without credentials looks healthy, then fails as a webhook rejecting
    every delivery while payments pile up unprocessed. Names the missing
    settings, never their values.

    base_url is checked too because the chart sets it unconditionally: an unset
    one arrives empty rather than absent, which overrides the default here
    instead of falling back to it.
    """
    if not enabled:
        return
    missing = [
        name
        for name, value in (
            ("flash_api_key", api_key),
            ("flash_webhook_secret", webhook_secret),
            ("flash_base_url", base_url),
        )
        if not value
    ]
    if missing:
        raise FlashConfigError(
            "flash_enabled is true but these settings are empty: " + ", ".join(missing)
        )
