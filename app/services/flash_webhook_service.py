"""Receiving inbound Flash subscription webhooks.

Flash signs each delivery with an HMAC over ``{timestamp}.{raw body}``, sent as
``Flash-Signature: t=<unix>,v1=<hex>``. Signatures are checked against the exact
bytes received — re-serializing the JSON would change them and every delivery
would fail.

Once the signature passes we know Flash sent it, so from that point the rule is
**record everything, reject nothing**: Flash retries a failed delivery only a few
times and then never sends it again.
"""

import hashlib
import hmac
import json
from dataclasses import dataclass
from datetime import datetime

from sqlalchemy.ext.asyncio import AsyncSession as AsyncDBSession

from app.core.loggr import loggr
from app.repos.billing_repo import insert_flash_webhook_event_on_db

logger = loggr.get_logger(__name__)

SIGNATURE_HEADER = "Flash-Signature"

# Recorded in place of the event name when a signed body doesn't carry one.
MALFORMED_EVENT = "_malformed"

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
class RecordedDelivery:
    event: str
    subscription_id: str | None
    duplicate: bool


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
    raw = payload.get("timestamp")
    if not isinstance(raw, str):
        return None
    try:
        return datetime.fromisoformat(raw.replace("Z", "+00:00")).replace(tzinfo=None)
    except ValueError:
        return None


def verify_delivery(
    *,
    signature_header: str | None,
    raw_body: bytes,
    secret: str,
    now: int,
    tolerance_seconds: int,
) -> SignatureParts:
    """Prove the delivery came from Flash. Raises FlashSignatureError otherwise."""
    parts = parse_signature_header(signature_header)
    if parts is None:
        raise FlashSignatureError("malformed_signature_header", 400)
    if not is_timestamp_fresh(parts.timestamp, now, tolerance_seconds):
        raise FlashSignatureError("stale_timestamp", 400)
    if not signature_matches(secret, parts.timestamp, raw_body, parts.signature):
        raise FlashSignatureError("invalid_signature", 401)
    return parts


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
    subscription_id = data.get("subscriptionId")

    inserted = await insert_flash_webhook_event_on_db(
        db,
        event=event,
        delivery_timestamp=delivery_timestamp,
        event_timestamp=parse_event_timestamp(payload),
        subscription_id=subscription_id,
        payload=payload,
        dedupe_key=build_dedupe_key(event, data, raw_body),
    )
    await db.commit()

    logger.info(
        "Flash webhook %s recorded (subscription=%s, duplicate=%s)",
        event,
        subscription_id,
        not inserted,
    )
    return RecordedDelivery(
        event=event, subscription_id=subscription_id, duplicate=not inserted
    )


def validate_flash_config(enabled: bool, api_key: str, webhook_secret: str) -> None:
    """Refuse to start half-configured.

    Booting without credentials looks healthy, then fails as a webhook rejecting
    every delivery while payments pile up unprocessed. Names the missing
    settings, never their values.
    """
    if not enabled:
        return
    missing = [
        name
        for name, value in (
            ("flash_api_key", api_key),
            ("flash_webhook_secret", webhook_secret),
        )
        if not value
    ]
    if missing:
        raise FlashConfigError(
            "flash_enabled is true but these settings are empty: " + ", ".join(missing)
        )
