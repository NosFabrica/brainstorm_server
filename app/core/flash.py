"""Reading subscription state from Flash.

Flash's own view is the authority on who is paid — not the webhook body, which
omits the period boundaries and can arrive out of order. Everything that grants
or revokes entitlement reads through here.

The distinction this module exists to protect: "Flash says they are not a
subscriber" is a fact, returned as None and acted on. "We could not ask Flash"
is not, and raises — because confusing the two revokes a paying user over a
socket timeout.
"""

import asyncio
from dataclasses import dataclass
from datetime import datetime, timezone

import httpx

from app.core.config import settings
from app.core.loggr import loggr

logger = loggr.get_logger(__name__)

# One shared client, lazily built and reused — same pattern as app/core/vespa.py.
_client: httpx.AsyncClient | None = None

CONNECT_RETRIES = 3
# Transient failures worth one more ask: the connection never happened, the
# response never finished, or Flash itself answered 5xx. A 4xx is an answer.
_RETRYABLE = (
    httpx.ConnectTimeout,
    httpx.ConnectError,
    httpx.PoolTimeout,
    httpx.ReadTimeout,
    httpx.ReadError,
)


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


class FlashCredentialError(Exception):
    """Flash refused our credentials.

    Deliberately not a subclass of FlashUnavailable: both leave every policy
    alone, but this one will fail identically forever, so it must not be retried
    and must not be buried among transient noise.
    """


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


def parse_flash_timestamp(raw: object) -> datetime | None:
    """One ISO string → naive UTC, the epoch every billing column stores.

    Converted to UTC before the tzinfo strip: Flash sends `Z` today, but a
    non-zero offset stripped naively would be silently wrong by that offset.
    A timestamp with no offset at all is taken as already UTC.
    """
    if not isinstance(raw, str):
        return None
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        logger.warning("Flash sent an unparseable timestamp; treating it as absent")
        return None
    if parsed.tzinfo is not None:
        parsed = parsed.astimezone(timezone.utc)
    return parsed.replace(tzinfo=None)


def parse_subscription(raw: dict) -> FlashSubscription:
    """Map one entry of the subscriptions response onto our shape."""
    return FlashSubscription(
        id=str(raw.get("id") or ""),
        status=str(raw.get("status") or ""),
        ref=raw.get("ref"),
        subscriber_id=raw.get("subscriberId"),
        service_id=str(raw.get("serviceId") or ""),
        plan_id=str(raw.get("planId") or ""),
        current_period_start=parse_flash_timestamp(raw.get("currentPeriodStart")),
        current_period_end=parse_flash_timestamp(raw.get("currentPeriodEnd")),
        next_billing_date=parse_flash_timestamp(raw.get("nextBillingDate")),
        trial_end_date=parse_flash_timestamp(raw.get("trialEndDate")),
        cancel_effective_date=parse_flash_timestamp(raw.get("cancelEffectiveDate")),
    )


_SUBSCRIPTIONS_PATH = "/api/v1/external/subscriptions"


async def _get_with_retries(url: str, params: dict) -> httpx.Response:
    """GET, retrying transient failures and 5xx with a short backoff."""
    client = _get_client()
    for attempt in range(CONNECT_RETRIES + 1):
        try:
            response = await client.get(
                url,
                params=params,
                headers={"Authorization": f"Bearer {settings.flash_api_key}"},
            )
        except _RETRYABLE as failed:
            if attempt == CONNECT_RETRIES:
                raise FlashUnavailable(f"Could not reach Flash: {failed}") from failed
        else:
            if response.status_code < 500 or attempt == CONNECT_RETRIES:
                return response
        await asyncio.sleep(0.1 * (attempt + 1))
    raise AssertionError("unreachable")


async def fetch_subscription(
    *, subscription_id: str | None = None, ref: str | None = None
) -> FlashSubscription | None:
    """Look a subscription up by Flash's id, or by our own reference.

    Returns None when Flash has no such subscription — a fact. Raises when we
    could not find out, which is not.

    Results are scoped by Flash to the account owning the API key, so a
    subscription belonging to someone else is not reachable with our credentials.
    """
    if not subscription_id and not ref:
        raise FlashUnavailable("A subscription lookup needs a subscriptionId or a ref")

    if settings.flash_mock_enabled:
        from app.core import flash_mock

        return flash_mock.lookup(subscription_id, ref)

    params = (
        {"subscriptionId": subscription_id} if subscription_id else {"ref": ref}
    )

    url = settings.flash_base_url.rstrip("/") + _SUBSCRIPTIONS_PATH
    try:
        response = await _get_with_retries(url, params)
    except FlashUnavailable:
        raise
    except Exception as failed:
        # Catch-all on purpose. A bare httpx error escaping here (InvalidURL is
        # not an HTTPError, for one) would abort a whole reconcile batch instead
        # of being recorded against one subscriber.
        raise FlashUnavailable(f"Could not reach Flash: {failed!r}") from failed

    if response.status_code in (401, 403):
        # Never echo the body or the key — this is the error most likely to be
        # pasted into a ticket.
        raise FlashCredentialError(
            f"Flash refused our credentials ({response.status_code})"
        )
    if response.status_code >= 400:
        raise FlashUnavailable(f"Flash answered {response.status_code}")

    try:
        body = response.json()
    except ValueError as failed:
        raise FlashUnavailable("Flash sent a body we could not read") from failed
    if not isinstance(body, dict):
        raise FlashUnavailable("Flash sent a body we could not read")

    if body.get("livemode") is False:
        # There is no Flash sandbox today, so this should not happen — but a
        # test-mode key granting real paid tiers is worth noticing loudly.
        logger.error("Flash answered in test mode; the API key may be wrong")

    subscriptions = body.get("subscriptions") if isinstance(body, dict) else None
    if not isinstance(subscriptions, list) or not subscriptions:
        return None

    chosen = _choose_subscription(subscriptions, subscription_id)
    if chosen is None:
        return None
    try:
        return parse_subscription(chosen)
    except (AttributeError, TypeError) as failed:
        raise FlashUnavailable("Flash sent a subscription we could not read") from failed


def _choose_subscription(
    subscriptions: list, subscription_id: str | None
) -> dict | None:
    """Pick the one we asked about.

    Flash returns an array and documents no ordering, and a re-subscribe leaves
    more than one row under a single `ref` — so taking the first would sometimes
    hand back the expired one and revoke someone who is paying.

    Asked by id, only that id will do. Asked by ref, prefer a subscription that
    still entitles; among several, the one that runs longest.
    """
    rows = [row for row in subscriptions if isinstance(row, dict)]
    if not rows:
        return None

    if subscription_id:
        return next((row for row in rows if row.get("id") == subscription_id), None)

    live = [row for row in rows if row.get("status") in ("active", "trial")] or rows
    return max(live, key=lambda row: str(row.get("currentPeriodEnd") or ""))
