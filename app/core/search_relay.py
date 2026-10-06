"""Client for the shared search relay's scored `/ore/search` route.

The relay returns each hit's own search relevance plus the Observer's lens
(whether that Observer's trust could be applied). Hits arrive highest score
first and are empty unless the lens is READY.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal, get_args

import httpx

from app.core.config import settings

LensStatus = Literal["READY", "NO_LIST", "NO_CARDS"]

_TIMEOUT_SECONDS = 5.0

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
    """The relay could not answer: network error, timeout, non-200 or a body
    outside the contract."""


@dataclass(frozen=True)
class Lens:
    status: LensStatus
    service: str | None


@dataclass(frozen=True)
class ScoredHit:
    pubkey: str
    score: float


@dataclass(frozen=True)
class OreSearchResult:
    hits: list[ScoredHit]
    lens: Lens


async def ore_search(q: str, observer: str, limit: int) -> OreSearchResult:
    try:
        response = await _get_client().get(
            "/ore/search", params={"q": q, "observer": observer, "limit": limit}
        )
    except httpx.HTTPError as e:
        raise SearchRelayUnavailable(f"search relay unreachable: {e!r}") from e
    if response.status_code != 200:
        raise SearchRelayUnavailable(
            f"search relay answered HTTP {response.status_code}"
        )
    try:
        body = response.json()
        lens = Lens(status=body["lens"]["status"], service=body["lens"]["service"])
        hits = [ScoredHit(str(h["pubkey"]), float(h["score"])) for h in body["hits"]]
    except (ValueError, KeyError, TypeError) as e:
        raise SearchRelayUnavailable(
            f"search relay answered outside the contract: {e!r}"
        ) from e
    if lens.status not in get_args(LensStatus):
        raise SearchRelayUnavailable(f"search relay sent unknown lens {lens.status!r}")
    return OreSearchResult(hits=hits, lens=lens)
