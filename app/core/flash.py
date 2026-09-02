"""Reading subscription state from Flash.

Flash's own view is the authority on who is paid — not the webhook body, which
omits the period boundaries and can arrive out of order. Everything that grants
or revokes entitlement reads through here.

The distinction this module exists to protect: "Flash says they are not a
subscriber" is a fact, returned as None and acted on. "We could not ask Flash"
is not, and raises — because confusing the two revokes a paying user over a
socket timeout.

Three reads, and which side of that line a 404 falls on differs between them:
`GET /subscriptions/{id}` and `/{id}/verify` are path lookups, where a 404 is
Flash answering — no such subscription. `GET /subscriptions?ref=` is a filtered
list, where a 404 is our URL being wrong and no answer about anybody.
"""

import asyncio
from dataclasses import dataclass
from datetime import date, datetime, time, timezone
from urllib.parse import quote

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


class FlashServiceMissing(Exception):
    """Flash holds no service under the id one of our plan mappings names.

    Not FlashUnavailable: Flash answered, and the answer is that our
    configuration points at nothing. Serving an empty pricing page for that
    would hide a fault only an operator can fix — so the id is carried on the
    exception, because it is the one thing whoever fixes it needs.
    """

    def __init__(self, service_id: str):
        self.service_id = service_id
        super().__init__(f"Flash holds no service {service_id!r}")


class FlashCredentialError(Exception):
    """Flash refused our credentials.

    Deliberately not a subclass of FlashUnavailable: both leave every policy
    alone, but this one will fail identically forever, so it must not be retried
    and must not be buried among transient noise.
    """


@dataclass(frozen=True)
class FlashPricing:
    """What Flash recorded this subscriber being charged, as at signup.

    Flash's `pricingSnapshot`, and the only honest answer to "what am I
    paying" — `FlashPlan` answers "what is on sale today", which is a different
    question the moment anyone reprices a plan.

    `amount_minor` is None when Flash sent an amount we could not read, for the
    same reason as `FlashPlan`: zero reads as "Free" to somebody being charged.
    """

    amount_minor: int | None
    currency: str | None
    billing_interval: str | None


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
    # Where Flash tells this subscriber to go and manage it. Today that is
    # their service portal, which is the URL we used to spell ourselves — but
    # spelling it was us guessing at their routing, and this is them answering.
    portal_url: str | None
    # None when Flash sent no snapshot at all, which is how a subscription with
    # no recorded price stays unpriced instead of borrowing the plan's.
    pricing: FlashPricing | None


START_OF_DAY = time(0, 0)
END_OF_DAY = time(23, 59, 59, 999999)


def _is_date_only(raw: str) -> bool:
    try:
        date.fromisoformat(raw)
    except ValueError:
        return False
    return True


def is_whole_day_boundary(value: datetime) -> bool:
    """Whether this is a boundary Flash named as a bare date — its first moment
    or its last. The stored value is the only record we keep of the shape it
    arrived in, so it is also the discriminator; see `_billing_date_wire_format`.
    """
    return value.time() in (START_OF_DAY, END_OF_DAY)


def parse_flash_timestamp(raw: object, *, deadline: bool = False) -> datetime | None:
    """One ISO string → naive UTC, the epoch every billing column stores.

    Converted to UTC before the tzinfo strip: Flash sends `Z` today, but a
    non-zero offset stripped naively would be silently wrong by that offset.
    A timestamp with no offset at all is taken as already UTC.

    Flash sends the period boundaries as bare dates, which ISO parsing promotes
    to midnight — reading a `deadline` as ending the instant its last day begins
    and revoking a subscriber up to a day early. So a deadline with no time runs
    to the end of its day. Only deadlines: a start really does mean 00:00.

    The discriminator is the absence of a time, not the value being midnight, so
    a genuine `T00:00:00Z` cannot gain a day — and the branch stops firing on its
    own once Flash sends instants.
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
    parsed = parsed.replace(tzinfo=None)
    if deadline and _is_date_only(raw):
        return datetime.combine(parsed.date(), END_OF_DAY)
    return parsed


# The two account policies our behaviour hard-codes, and where it does so.
# Flash now reports both on every subscription, so the assumptions can be
# checked instead of believed — reported, never acted on: a policy read wrongly
# must not be able to move anyone's tier.
_ASSUMED_CANCELLATION_MODE = "end_of_period"
_reported_policies: set[str] = set()


def _report_policy_differences(raw: dict) -> None:
    mode = (raw.get("cancellationPolicy") or {}).get("mode")
    if mode is not None and mode != _ASSUMED_CANCELLATION_MODE:
        _report_once(
            f"cancellation:{mode}",
            "Flash cancels %s, but we keep a cancelled subscriber entitled "
            "until cancelEffectiveDate or the period end. Behaviour unchanged.",
            mode,
        )

    # past_due entitles because Flash is retrying and the retrying ends. A
    # policy that never retries, or never gives up, breaks one half of that.
    dunning = raw.get("dunningPolicy") or {}
    attempts = dunning.get("maxAttempts")
    if isinstance(attempts, int) and attempts < 1:
        _report_once(
            f"dunning-attempts:{attempts}",
            "Flash retries a failed renewal %s time(s), but we read past_due as "
            "still entitled because it is being retried. Behaviour unchanged.",
            attempts,
        )
    if dunning.get("cancelAfterFinalFailure") is False:
        _report_once(
            "dunning-never-ends",
            "Flash leaves a subscription past_due after the final failed "
            "retry, but we read past_due as still entitled. Behaviour "
            "unchanged.",
        )


def _report_once(key: str, message: str, *args: object) -> None:
    """Once per difference: the policy is the account's, so every row of a
    reconcile pass carries the same one."""
    if key in _reported_policies:
        return
    _reported_policies.add(key)
    logger.warning(message, *args)


def parse_subscription(raw: dict) -> FlashSubscription:
    """Map one entry of the subscriptions response onto our shape. Pure."""
    return FlashSubscription(
        id=str(raw.get("id") or ""),
        status=str(raw.get("status") or ""),
        ref=raw.get("ref"),
        subscriber_id=raw.get("subscriberId"),
        service_id=str(raw.get("serviceId") or ""),
        plan_id=str(raw.get("planId") or ""),
        current_period_start=parse_flash_timestamp(raw.get("currentPeriodStart")),
        current_period_end=parse_flash_timestamp(
            raw.get("currentPeriodEnd"), deadline=True
        ),
        next_billing_date=parse_flash_timestamp(raw.get("nextBillingDate")),
        trial_end_date=parse_flash_timestamp(raw.get("trialEndDate"), deadline=True),
        cancel_effective_date=parse_flash_timestamp(
            raw.get("cancelEffectiveDate"), deadline=True
        ),
        portal_url=_text(raw.get("portalUrl")),
        pricing=parse_pricing(raw.get("pricingSnapshot")),
    )


def parse_pricing(raw: object) -> FlashPricing | None:
    """Flash's `pricingSnapshot`, or None when they sent none. Pure."""
    if not isinstance(raw, dict):
        return None
    return FlashPricing(
        amount_minor=_minor_units(raw.get("amount")),
        currency=_text(raw.get("currency")),
        billing_interval=_text(raw.get("billingInterval")),
    )


_SUBSCRIPTIONS_PATH = "/api/v1/external/subscriptions"
_SERVICES_PATH = "/api/v1/external/services"


def _subscriptions_url(*segments: str) -> str:
    base = settings.flash_base_url.rstrip("/") + _SUBSCRIPTIONS_PATH
    return "/".join([base, *(quote(segment, safe="") for segment in segments)])


def _services_url(*segments: str) -> str:
    base = settings.flash_base_url.rstrip("/") + _SERVICES_PATH
    return "/".join([base, *(quote(segment, safe="") for segment in segments)])


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


def _require_a_handle(subscription_id: str | None, ref: str | None) -> None:
    """Flash supports exactly two lookups, and never both at once."""
    if not subscription_id and not ref:
        raise FlashUnavailable("A subscription lookup needs a subscriptionId or a ref")


async def _read_body(
    url: str, params: dict, *, absent_on_404: bool = False
) -> dict | None:
    """The one request every lookup makes, and the one place its failures are named.

    Shared so the parsed and raw reads cannot drift on what counts as "Flash
    said no" versus "we could not ask", which is the distinction this module
    exists to protect.

    `absent_on_404` belongs to the path lookups only. There, a 404 is Flash
    answering clearly — no such subscription — and returning None says so.
    On the filtered list a 404 is our URL being wrong, which is not an answer.
    """
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
    if response.status_code == 404 and absent_on_404:
        return None
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

    return body


async def fetch_subscription(
    *, subscription_id: str | None = None, ref: str | None = None
) -> FlashSubscription | None:
    """Look a subscription up by Flash's id, or by our own reference.

    Returns None when Flash has no such subscription — a fact. Raises when we
    could not find out, which is not.

    Results are scoped by Flash to the account owning the API key, so a
    subscription belonging to someone else is not reachable with our credentials.
    """
    _require_a_handle(subscription_id, ref)

    if settings.flash_mock_enabled:
        from app.core import flash_mock

        return flash_mock.lookup(subscription_id, ref)

    body = await _read_for(subscription_id, ref)
    if body is None:
        return None
    if subscription_id:
        return _subscription_from(body.get("subscription"))

    subscriptions = body.get("subscriptions")
    if not isinstance(subscriptions, list) or not subscriptions:
        return None

    return _subscription_from(_choose_subscription(subscriptions))


async def verify_subscription(subscription_id: str) -> FlashSubscription | None:
    """Check a checkout outcome the user's browser handed us.

    Anyone can type a URL, so the redirect's `subscriptionId` is a claim, not a
    grant. Flash's verification endpoint is the answer to it: the subscription
    when Flash calls it valid, None when it does not know it or does not — and
    still nothing at all when we could not ask.

    It answers whose subscription this is, not whether it is the caller's: the
    `ref` it carries is what the caller must be checked against.
    """
    if not subscription_id:
        raise FlashUnavailable("A verification needs a subscriptionId")

    if settings.flash_mock_enabled:
        from app.core import flash_mock

        return flash_mock.lookup(subscription_id, None)

    body = await _read_body(
        _subscriptions_url(subscription_id, "verify"), {}, absent_on_404=True
    )
    if not body or body.get("valid") is not True:
        return None
    return _subscription_from(body.get("subscription"))


async def _read_for(subscription_id: str | None, ref: str | None) -> dict | None:
    """Whichever endpoint the handle names, so both reads dispatch identically.

    An id is a path of its own and can come back absent. A ref is the filtered
    list, where an account with nothing in it is an empty array, not a 404.
    """
    if subscription_id:
        return await _read_body(
            _subscriptions_url(subscription_id), {}, absent_on_404=True
        )
    return await _read_body(_subscriptions_url(), {"ref": ref})


def _subscription_from(raw: object) -> FlashSubscription | None:
    """Parse one row of a Flash body, and read its policies while we hold them."""
    if raw is None:
        return None
    try:
        _report_policy_differences(raw)
        return parse_subscription(raw)
    except (AttributeError, TypeError) as failed:
        raise FlashUnavailable("Flash sent a subscription we could not read") from failed


async def fetch_subscription_raw(
    *, subscription_id: str | None = None, ref: str | None = None
) -> dict | None:
    """Flash's own answer, as it arrived — every row, not the one we would pick.

    `fetch_subscription` collapses a multi-row answer down to the subscription
    that decides entitlement. That collapse is the thing an operator comparing
    our stored row against Flash needs to see through, so every row survives
    here, each exactly as Flash sent it.

    One shape whichever handle was used: a path lookup returns its one
    subscription as a one-row `subscriptions` array, so the operator surface
    reads both the same way.

    Same contract otherwise — None means Flash holds no such subscription,
    raising means we could not find out.
    """
    _require_a_handle(subscription_id, ref)

    if settings.flash_mock_enabled:
        from app.core import flash_mock

        rows = flash_mock.lookup_raw(subscription_id, ref)
        return {"livemode": True, "subscriptions": rows} if rows else None

    body = await _read_for(subscription_id, ref)
    if body is None:
        return None
    if subscription_id:
        one = body.get("subscription")
        return {**body, "subscriptions": [one]} if one is not None else None

    subscriptions = body.get("subscriptions")
    if not isinstance(subscriptions, list) or not subscriptions:
        return None
    return body


def _choose_subscription(subscriptions: list) -> dict | None:
    """Pick, among everything held under one `ref`, the row that decides entitlement.

    Flash returns an array and documents no ordering, and a re-subscribe leaves
    more than one row under a single `ref` — so taking the first would sometimes
    hand back the expired one and revoke someone who is paying. Prefer a
    subscription that still entitles; among several, the one that runs longest.

    Only the `ref` lookup gets here: an id is a path of its own, which returns
    the one subscription and nothing to choose between.
    """
    rows = [row for row in subscriptions if isinstance(row, dict)]
    if not rows:
        return None

    live = [row for row in rows if row.get("status") in ("active", "trial")] or rows
    return max(live, key=_runs_until)


@dataclass(frozen=True)
class FlashPlan:
    """One plan on one service, as Flash offers it.

    Everything a pricing card says except which scheduling policy it grants and
    whether we sell it — those two are ours, and are the reason `billing_plan`
    still exists.

    `status` is Flash's own: it says whether *Flash* offers the plan, never
    whether we do.
    """

    id: str
    service_id: str
    name: str
    description: str | None
    # None when Flash sent an amount we could not read. Deliberately not zero:
    # zero renders as "Free" on a public page for a plan somebody is charged
    # for, and a plan we cannot price is a plan we cannot sell.
    amount_minor: int | None
    currency: str
    billing_interval: str | None
    sort_order: int
    features: list[str] | None
    not_included: list[str] | None
    status: str
    # Flash's own hosted checkout for this plan. None when they send none, and
    # then the plan is as unsellable as one we could not price — we no longer
    # keep a way to spell the URL ourselves.
    signup_url: str | None


def parse_plan(raw: dict) -> FlashPlan:
    """Map one entry of a service's `plans` array onto our shape. Pure."""
    return FlashPlan(
        id=str(raw.get("id") or ""),
        service_id=str(raw.get("serviceId") or ""),
        name=str(raw.get("name") or ""),
        description=_text(raw.get("description")),
        amount_minor=_minor_units(raw.get("amount")),
        currency=str(raw.get("currency") or ""),
        billing_interval=_text(raw.get("billingInterval")),
        sort_order=raw.get("sortOrder") if isinstance(raw.get("sortOrder"), int) else 0,
        features=_lines(raw.get("features")),
        not_included=_lines(raw.get("notIncluded")),
        status=str(raw.get("status") or ""),
        signup_url=_text(raw.get("signupUrl")),
    )


def _text(value: object) -> str | None:
    return value.strip() or None if isinstance(value, str) else None


def _minor_units(value: object) -> int | None:
    """Flash sends amounts as strings in minor units (`"100"` = $1.00).

    Unreadable is None, never zero and never an exception. Zero would price the
    plan "Free" on a public page; raising would take the page down for one bad
    row. None says we do not know, and a plan nobody can price is withdrawn
    from sale by the caller.
    """
    try:
        return int(str(value))
    except (TypeError, ValueError):
        logger.warning("Flash sent a plan amount we could not read: %r", value)
        return None


def _lines(value: object) -> list[str] | None:
    if not isinstance(value, list):
        return None
    lines = [item.strip() for item in value if isinstance(item, str) and item.strip()]
    return lines or None


async def fetch_service_plans_raw(service_id: str) -> dict:
    """`GET /services/{id}` — the plans Flash offers on one service.

    A path lookup, so a 404 is Flash answering: there is no such service. That
    is not an empty catalogue but a plan mapping pointing at nothing, so it
    raises rather than returning absence.
    """
    if not service_id:
        raise FlashUnavailable("A service read needs a serviceId")

    body = await _read_body(_services_url(service_id), {}, absent_on_404=True)
    if body is None:
        raise FlashServiceMissing(service_id)
    return body


def _runs_until(row: dict) -> datetime:
    """How long a row entitles, as a moment — never as the string Flash sent.

    Compared as text, a bare `2026-09-20` sorts below `2026-09-20T00:00:01Z`,
    so the row with a whole day left would lose to one a second past midnight.
    Read as a deadline, for the same reason `current_period_end` is.
    """
    return parse_flash_timestamp(row.get("currentPeriodEnd"), deadline=True) or datetime.min
