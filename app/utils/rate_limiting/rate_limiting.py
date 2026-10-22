import ipaddress
from collections.abc import Awaitable, Callable
from dataclasses import dataclass

from fastapi import HTTPException, Request

from app.core.config import settings
from app.core.loggr import loggr
from app.core.redis_db import get_redis_client

logger = loggr.get_logger(__name__)


@dataclass(frozen=True)
class RateLimitPolicy:
    """A named throttle: which bucket, how many requests, over what window."""

    key_prefix: str
    limit: int
    window_seconds: int


# Shared by POST /user/graperank and POST /user/followList (pre-existing).
GRAPERANK_POLICY = RateLimitPolicy(key_prefix="graperank", limit=3, window_seconds=1800)


async def _enforce_window(key: str, limit: int, window_seconds: int) -> None:
    redis_client = get_redis_client()
    current = await redis_client.incr(key)
    if current == 1:
        await redis_client.expire(key, window_seconds)
    if current > limit:
        retry_after = await redis_client.ttl(key)
        if retry_after < 1:
            retry_after = window_seconds
        logger.warning(
            "rate limit hit: bucket=%s limit=%d/%ds", key, limit, window_seconds
        )
        raise HTTPException(
            status_code=429,
            detail="Too many requests",
            headers={"Retry-After": str(retry_after)},
        )


def resolve_client_ip(request: Request) -> str:
    """The X-Forwarded-For hop our own proxy wrote; the leading entries are client-controlled."""
    hops_back = settings.trusted_proxy_hops
    forwarded = request.headers.get("x-forwarded-for", "")
    hops = [hop.strip() for hop in forwarded.split(",") if hop.strip()]

    if 1 <= hops_back <= len(hops):
        return hops[-hops_back]

    if hops:
        logger.warning(
            "X-Forwarded-For carried %d hop(s) but trusted_proxy_hops=%d; "
            "falling back to the direct peer, which is shared behind a proxy "
            "and will throttle unrelated callers together",
            len(hops),
            hops_back,
        )

    return request.client.host if request.client else "unknown"


def client_bucket(ip_address: str) -> str:
    """IPv6 callers are bucketed by /64: one host normally holds the whole prefix."""
    try:
        address = ipaddress.ip_address(ip_address)
    except ValueError:
        return ip_address
    if address.version == 4:
        return str(address)
    if address.ipv4_mapped:
        return str(address.ipv4_mapped)
    return str(ipaddress.ip_network(f"{address}/64", strict=False))


async def validate_rate_limit(ip_address: str, policy: RateLimitPolicy) -> None:
    """Fixed-window rate limit per IP; 429 once the window's count exceeds the limit."""
    await _enforce_window(
        f"rate_limit:{policy.key_prefix}:{client_bucket(ip_address)}",
        policy.limit,
        policy.window_seconds,
    )


def rate_limit(policy: RateLimitPolicy):
    """A FastAPI dependency enforcing ``policy`` per client IP."""

    async def dependency(request: Request) -> None:
        await validate_rate_limit(resolve_client_ip(request), policy)

    return dependency


def rate_limit_per_ip(
    key_prefix: str, limit: Callable[[], int], window_seconds: int
) -> Callable[[Request], Awaitable[None]]:
    """Like ``rate_limit``, but ``limit`` is read on every request (a setting)."""

    async def dependency(request: Request) -> None:
        await validate_rate_limit(
            resolve_client_ip(request),
            RateLimitPolicy(key_prefix, limit(), window_seconds),
        )

    return dependency


rate_limit_login_challenge = rate_limit_per_ip(
    "login_challenge", lambda: settings.login_challenge_rate_limit_per_minute, 60
)
rate_limit_login_verify = rate_limit_per_ip(
    "login_verify", lambda: settings.login_verify_rate_limit_per_minute, 60
)
# Bounds Observer rows minted from throwaway keypairs.
rate_limit_login_new_observer = rate_limit_per_ip(
    "login_new_observer", lambda: settings.login_new_observer_rate_limit_per_hour, 3600
)


# Generous enough for the pending-checkout poll (every few seconds), tight
# enough that nobody hammers Flash on our credentials through us.
REFRESH_RATE_LIMIT = 12
REFRESH_WINDOW_SECONDS = 60


async def validate_subscription_refresh_allowed(pubkey: str) -> None:
    await _enforce_window(
        f"rate_limit:billing_refresh:{pubkey}",
        REFRESH_RATE_LIMIT,
        REFRESH_WINDOW_SECONDS,
    )


# An operator clicking through a report, not a poll — and every click spends our
# Flash quota on our credentials, so a stuck menu can't become an incident.
FLASH_RECORD_RATE_LIMIT = 30
FLASH_RECORD_WINDOW_SECONDS = 60


async def validate_flash_record_read_allowed(operator_pubkey: str) -> None:
    await _enforce_window(
        f"rate_limit:billing_flash_record:{operator_pubkey}",
        FLASH_RECORD_RATE_LIMIT,
        FLASH_RECORD_WINDOW_SECONDS,
    )


# Room for a real back-and-forth inside one sitting; the open-ticket cap bounds
# how many threads exist, nothing else bounds how much is written into them.
SUPPORT_MESSAGE_RATE_LIMIT = 20
SUPPORT_MESSAGE_WINDOW_SECONDS = 60


async def validate_support_message_allowed(pubkey: str) -> None:
    await _enforce_window(
        f"rate_limit:support_message:{pubkey}",
        SUPPORT_MESSAGE_RATE_LIMIT,
        SUPPORT_MESSAGE_WINDOW_SECONDS,
    )
