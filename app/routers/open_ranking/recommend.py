"""ORE-04: POST /recommend/pubkeys — merchants a point of view trusts.

The candidates are the authors of kind-30402 product listings (NIP-99 / Open
Markets), read from the search relay with the observer as lens: the relay
returns only listings from authors above the observer's trust floor, ordered
by that trust (`sort:rank`). The `-topic` algorithms declare a `topic` and keep
the listings whose text matches it; the others ignore one. Ranks are the
observer's GrapeRank influence, the same number ORE-03 answers, so a merchant
ranks the same on both endpoints. The relay's gate is the only trust filter:
influence orders the sellers it let through and never removes one.
"""

from __future__ import annotations

import re
import time
from collections import OrderedDict

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession as AsyncDBSession

from app.core.database import get_db
from app.core.search_relay import SearchRelayUnavailable, fetch_events
from app.neo4j_db.driver import driver as neo4j_driver
from app.repos.user_repo import batch_influence_for_pubkeys
from app.routers.open_ranking.availability import ensure_pov_available
from app.routers.open_ranking.capabilities import (
    algorithm_requires_topic,
    resolve_algorithm,
)
from app.routers.open_ranking.common import (
    DEFAULT_RECOMMEND_LIMIT,
    TTL_HINTS,
    validate_positive_limit,
    validate_pubkey,
)
from app.routers.open_ranking.rank import _safe_rank
from app.routers.open_ranking.schemas import (
    RankResult,
    RecommendPubkeysRequest,
    RecommendPubkeysResponse,
    ore_responses,
)
from app.utils.auth.nwt import optional_nwt_signer

router = APIRouter()

ENDPOINT = "/recommend/pubkeys"
LISTING_KIND = 30402

# ORE-04: clients SHOULD NOT send a longer topic; the provider caps it.
MAX_TOPIC_LENGTH = 512

# Listings are ordered by their author's trust, so one prolific merchant can
# fill many slots; asking for this many listings per merchant wanted keeps the
# page wide enough. The relay caps one read at 1000.
LISTINGS_PER_MERCHANT = 5
MAX_LISTINGS = 1000

# A NIP-50 extension token (`include:spam`, `observer:<hex>`): a topic word of
# this shape could override the lens. `10:30` and URLs are ordinary words.
_EXTENSION_TOKEN = re.compile(r"^[A-Za-z][A-Za-z0-9_-]*:(?!//)")

# Answers are reused this long: listings and influence move on the scale of
# GrapeRank runs, and the relay serves every caller from two NIP-FE slots.
CACHE_SECONDS = 300
_CACHE_ENTRIES = 1024
_cache: OrderedDict[
    tuple[str, str, int], tuple[float, list[RankResult]]
] = OrderedDict()


def _topic_terms(topic: str | None) -> str:
    """The topic as NIP-50 words. Quotes are dropped, because an unclosed one
    would swallow the lens tokens appended after it into a phrase."""
    if not topic or not topic.strip():
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail="this algorithm requires a 'topic'",
        )
    if len(topic) > MAX_TOPIC_LENGTH:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail=f"topic exceeds the maximum length of {MAX_TOPIC_LENGTH}",
        )
    words = [
        w for w in topic.replace('"', " ").split() if not _EXTENSION_TOKEN.match(w)
    ]
    if not words:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail="topic is not recognised: it holds no searchable words",
        )
    return " ".join(words)


async def listing_authors(observer: str, topic: str, want: int) -> list[str]:
    """Every distinct author on the relay's page of trusted listings."""
    search = " ".join(
        part for part in (topic, f"sort:rank observer:{observer}") if part
    )
    listings = await fetch_events(
        {
            "kinds": [LISTING_KIND],
            "search": search,
            "limit": min(MAX_LISTINGS, want * LISTINGS_PER_MERCHANT),
        }
    )
    authors = (listing.get("pubkey") for listing in listings)
    return list(dict.fromkeys(a for a in authors if isinstance(a, str)))


async def trusted_merchants(observer: str, topic: str, limit: int) -> list[RankResult]:
    """The page's authors ranked by influence, then cut: the relay's order is
    by match tier and a 0..100 rank with many ties, so it never decides the cut
    between scored sellers. It still orders the ones influence cannot tell
    apart, including those with no score at all."""
    key = (observer, topic, limit)
    hit = _cache.get(key)
    if hit is not None and hit[0] > time.monotonic():
        _cache.move_to_end(key)
        return hit[1]

    authors = await listing_authors(observer, topic, limit)
    async with neo4j_driver.session() as session:
        influence = await batch_influence_for_pubkeys(session, authors, observer)

    # The relay's lens is the gate: every author here passed the pov's trust
    # floor. Influence only orders them. A seller our graph has no score for
    # (a 30382 left on the relay from before a recompute, or a pov whose 10040
    # names another provider) keeps its place after the scored ones at rank 0
    # rather than being dropped, which would empty a foreign pov's answer.
    ranked = [
        RankResult(pubkey=pk, rank=_safe_rank(influence.get(pk))) for pk in authors
    ]
    ranked.sort(key=lambda r: -r.rank)  # stable: ties keep the relay's order
    results = ranked[:limit]

    _cache[key] = (time.monotonic() + CACHE_SECONDS, results)
    _cache.move_to_end(key)
    while len(_cache) > _CACHE_ENTRIES:
        _cache.popitem(last=False)
    return results


@router.post(
    ENDPOINT,
    response_model=RecommendPubkeysResponse,
    summary="ORE-04: merchants a point of view trusts",
    responses=ore_responses(unavailable="The search relay is unreachable or off-spec."),
)
async def recommend_pubkeys(
    req: RecommendPubkeysRequest,
    signer: str | None = Depends(optional_nwt_signer),
    db: AsyncDBSession = Depends(get_db),
) -> RecommendPubkeysResponse:
    pov = validate_pubkey(req.pov, "pov") if req.pov else None
    limit = validate_positive_limit(req.limit) or DEFAULT_RECOMMEND_LIMIT

    algo_id, observer = resolve_algorithm(
        ENDPOINT, req.algorithm, pov, forced_observer=signer
    )
    # As a pov is for a global algorithm, a topic is ignored where none is declared.
    topic = (
        _topic_terms(req.topic) if algorithm_requires_topic(ENDPOINT, algo_id) else ""
    )
    await ensure_pov_available(db, ENDPOINT, algo_id, observer)

    try:
        results = await trusted_merchants(observer, topic, limit)
    except SearchRelayUnavailable as e:
        headers = {"Retry-After": e.retry_after} if e.retry_after else None
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=str(e),
            headers=headers,
        ) from e
    return RecommendPubkeysResponse(results=results, ttl=TTL_HINTS[ENDPOINT])
