"""Client for the shared search relay: its public `POST /search/pubkeys`
backend, and single NIP-50 reads over NIP-FE (a client frame POSTed to `/`).

We are the Open Ranking provider; ORE-05 profile search forwards to the
relay's route and passes its answer through unchanged. ORE-04 reads listings
through `fetch_events` and ranks what comes back itself.
"""

from __future__ import annotations

import asyncio
import json
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
    outside the ORE-05 shape. `retry_after` is the relay's own hint, when it
    sent one with a 429 or 503."""

    def __init__(self, reason: str, retry_after: str | None = None):
        super().__init__(reason)
        self.retry_after = retry_after


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


# The subscription id of every NIP-FE read; nothing stays open, so one is enough.
_FE_SUBSCRIPTION = "ore"

# The relay runs at most HTTP_RELAY_PER_CLIENT (default 2) NIP-FE commands per
# client address and answers the next with 429. Every caller here shares this
# server's address, so reads queue for a slot instead of being refused.
_FE_SLOTS = asyncio.Semaphore(2)

# One read's whole budget: httpx's timeout bounds each socket read, not the
# answer, and the relay may stream for up to its own 30 s deadline.
_FE_BUDGET_SECONDS = 10.0


async def fetch_events(nostr_filter: dict) -> list[dict]:
    """One REQ over NIP-FE, its events in the relay's order (a `sort:` token in
    `search` decides it). A CLOSED or NOTICE is a refusal, not an empty answer,
    so it raises rather than reading as "no results"."""
    frame = json.dumps(["REQ", _FE_SUBSCRIPTION, nostr_filter])
    try:
        async with _FE_SLOTS, asyncio.timeout(_FE_BUDGET_SECONDS):
            response = await _get_client().post("/", content=frame)
    except (httpx.HTTPError, TimeoutError) as e:
        raise SearchRelayUnavailable(f"search relay unreachable: {e!r}") from e
    if response.status_code != 200:
        raise SearchRelayUnavailable(
            f"search relay answered HTTP {response.status_code}",
            retry_after=response.headers.get("Retry-After"),
        )

    events: list[dict] = []
    # NDJSON lines end at "\n" only: `splitlines()` also breaks on U+2028 and
    # friends, which JSON allows unescaped inside a string (profiles carry them).
    for line in response.text.split("\n"):
        if not line.strip():
            continue
        try:
            answer = json.loads(line)
        except ValueError as e:
            raise SearchRelayUnavailable(
                f"search relay sent a line that is not JSON: {e!r}"
            ) from e
        if not isinstance(answer, list) or not answer:
            raise SearchRelayUnavailable(
                "search relay sent a frame that is not a JSON array"
            )
        if answer[0] == "EVENT" and len(answer) >= 3 and isinstance(answer[2], dict):
            events.append(answer[2])
        elif answer[0] == "EOSE":
            return events
        elif answer[0] in ("CLOSED", "NOTICE"):
            raise SearchRelayUnavailable(
                f"search relay refused the read: {answer[-1]!r}"
            )
    raise SearchRelayUnavailable("search relay ended the answer without EOSE")


# The most authors one profile read names; 500 came back in ~1 s (2026-10-27).
PROFILE_BATCH = 500


async def fetch_profiles(pubkeys: list[str]) -> dict[str, dict]:
    """The latest kind-0 the relay holds for each of `pubkeys` (at most
    `PROFILE_BATCH`), by author; an author it has none for is absent.

    Read unranked (`include:spam`): this is a lookup by author, not a search, and
    without a ranking observer the relay refuses anonymous reads. No signature
    check — we run this relay, and it verifies what it stores. Raises
    `SearchRelayUnavailable` like `fetch_events`.
    """
    if not pubkeys:
        return {}
    events = await fetch_events(
        {
            "kinds": [0],
            "authors": pubkeys,
            "limit": len(pubkeys),
            "search": "include:spam",
        }
    )
    latest: dict[str, dict] = {}
    for event in events:
        author = event.get("pubkey")
        if not isinstance(author, str) or event.get("kind") != 0:
            continue
        held = latest.get(author)
        if held is None or event.get("created_at", 0) > held.get("created_at", 0):
            latest[author] = event
    return latest
