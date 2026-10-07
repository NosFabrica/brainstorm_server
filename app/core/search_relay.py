"""Client for the shared search relay's private `POST /search/pubkeys` backend.

We are the Open Ranking provider; ORE-05 profile search forwards to the
relay's route and passes its answer through unchanged.
"""

from __future__ import annotations

from dataclasses import dataclass

import httpx

from app.core.config import settings

_TIMEOUT_SECONDS = 5.0

_PASSED_HEADERS = ("Content-Type", "X-Reason", "Retry-After")

# Errors ORE-05 defines, plus the relay's busy / House-not-ready 503.
_ERROR_STATUSES = frozenset({400, 422, 503})

# One shared keep-alive client, lazily built — same pattern as app/core/flash.py.
_client: httpx.AsyncClient | None = None


def _get_client() -> httpx.AsyncClient:
    global _client
    if _client is None:
        _client = httpx.AsyncClient(
            base_url=settings.search_relay_url,
            timeout=httpx.Timeout(_TIMEOUT_SECONDS),
        )
    return _client


async def aclose() -> None:
    """Close the shared client. Called from the FastAPI lifespan shutdown."""
    global _client
    if _client is not None:
        await _client.aclose()
        _client = None


class SearchRelayUnavailable(Exception):
    """The relay could not answer: network error, timeout, or an answer
    outside the ORE-05 shape."""


@dataclass(frozen=True)
class RelayAnswer:
    status_code: int
    body: bytes
    headers: dict[str, str]


def _is_result(item: object) -> bool:
    if not isinstance(item, dict):
        return False
    rank = item.get("rank")
    return (
        isinstance(item.get("pubkey"), str)
        and isinstance(rank, (int, float))
        and not isinstance(rank, bool)
    )


def _ensure_spec_shape(response: httpx.Response) -> None:
    code = response.status_code
    if code in _ERROR_STATUSES:
        return
    if code == 202:
        if "Retry-After" not in response.headers:
            raise SearchRelayUnavailable("search relay sent 202 without Retry-After")
        return
    if code != 200:
        raise SearchRelayUnavailable(f"search relay answered HTTP {code}")
    try:
        results = response.json()["results"]
    except (ValueError, KeyError, TypeError) as e:
        raise SearchRelayUnavailable(
            f"search relay answered outside the ORE-05 shape: {e!r}"
        ) from e
    if not isinstance(results, list) or not all(_is_result(r) for r in results):
        raise SearchRelayUnavailable(
            "search relay answered outside the ORE-05 shape: bad results"
        )


async def search_pubkeys(body: bytes) -> RelayAnswer:
    try:
        response = await _get_client().post(
            "/search/pubkeys",
            content=body,
            headers={"Content-Type": "application/json"},
        )
    except httpx.HTTPError as e:
        raise SearchRelayUnavailable(f"search relay unreachable: {e!r}") from e
    _ensure_spec_shape(response)
    return RelayAnswer(
        status_code=response.status_code,
        body=response.content,
        headers={
            name: response.headers[name]
            for name in _PASSED_HEADERS
            if name in response.headers
        },
    )
