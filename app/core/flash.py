"""Reading subscription state from Flash.

Flash's own view is the authority on who is paid — not the webhook body, which
omits the period boundaries and can arrive out of order. Everything that grants
or revokes entitlement reads through here.

The read here is deliberately plain — one call, no retry taxonomy, no recovery
sweep. Hardening it is slice 04's job. What matters now is the invariant it
carries: any failure raises `FlashUnavailable`, and a caller that cannot read
Flash must leave every user's policy exactly as it found it.
"""

from dataclasses import dataclass
from datetime import datetime

import httpx

from app.core.config import settings
from app.core.loggr import loggr

logger = loggr.get_logger(__name__)

# One shared client, lazily built and reused — same pattern as app/core/vespa.py.
_client: httpx.AsyncClient | None = None


def _get_client() -> httpx.AsyncClient:
    global _client
    if _client is None:
        _client = httpx.AsyncClient(
            timeout=httpx.Timeout(settings.flash_http_timeout_seconds)
        )
    return _client


async def aclose() -> None:
    """Close the shared client. Called from the FastAPI lifespan shutdown."""
    global _client
    if _client is not None:
        await _client.aclose()
        _client = None


class FlashUnavailable(Exception):
    """Flash could not be read. Never a reason to move anyone's tier."""


@dataclass(frozen=True)
class FlashSubscription:
    """One subscriber on one plan, as Flash reports it.

    `status` is carried verbatim. Flash documents its set as open, so an
    unrecognised value must survive to be diagnosed rather than be coerced.
    """

    id: str
    status: str
    ref: str | None
    subscriber_id: str | None
    service_id: str
    plan_id: str
    current_period_start: datetime | None
    current_period_end: datetime | None
    next_billing_date: datetime | None
    trial_end_date: datetime | None
    cancel_effective_date: datetime | None


def _parse_timestamp(raw: object) -> datetime | None:
    if not isinstance(raw, str):
        return None
    try:
        return datetime.fromisoformat(raw.replace("Z", "+00:00")).replace(tzinfo=None)
    except ValueError:
        logger.warning("Flash sent an unparseable timestamp; treating it as absent")
        return None


def parse_subscription(raw: dict) -> FlashSubscription:
    """Map one entry of the subscriptions response onto our shape."""
    return FlashSubscription(
        id=str(raw.get("id") or ""),
        status=str(raw.get("status") or ""),
        ref=raw.get("ref"),
        subscriber_id=raw.get("subscriberId"),
        service_id=str(raw.get("serviceId") or ""),
        plan_id=str(raw.get("planId") or ""),
        current_period_start=_parse_timestamp(raw.get("currentPeriodStart")),
        current_period_end=_parse_timestamp(raw.get("currentPeriodEnd")),
        next_billing_date=_parse_timestamp(raw.get("nextBillingDate")),
        trial_end_date=_parse_timestamp(raw.get("trialEndDate")),
        cancel_effective_date=_parse_timestamp(raw.get("cancelEffectiveDate")),
    )


_SUBSCRIPTIONS_PATH = "/api/v1/external/subscriptions"


async def fetch_subscription(
    *, subscription_id: str | None = None, ref: str | None = None
) -> FlashSubscription | None:
    """Look a subscription up by Flash's id, or by our own reference.

    Returns None when Flash simply has no such subscription — a fact, not a
    failure. Anything that leaves us *unsure* raises `FlashUnavailable`, because
    the two must never be confused: one means "they are not a subscriber", the
    other means "do not touch anything".
    """
    if subscription_id:
        params = {"subscriptionId": subscription_id}
    elif ref:
        params = {"ref": ref}
    else:
        raise FlashUnavailable("A subscription lookup needs a subscriptionId or a ref")

    url = settings.flash_base_url.rstrip("/") + _SUBSCRIPTIONS_PATH
    try:
        response = await _get_client().get(
            url,
            params=params,
            headers={"Authorization": f"Bearer {settings.flash_api_key}"},
        )
        response.raise_for_status()
        body = response.json()
    except httpx.HTTPStatusError as failed:
        # Never log the response body — it carries subscriber PII.
        raise FlashUnavailable(
            f"Flash answered {failed.response.status_code}"
        ) from failed
    except (httpx.HTTPError, ValueError) as failed:
        raise FlashUnavailable(f"Could not read Flash: {failed}") from failed

    subscriptions = body.get("subscriptions") if isinstance(body, dict) else None
    if not subscriptions:
        return None
    return parse_subscription(subscriptions[0])
