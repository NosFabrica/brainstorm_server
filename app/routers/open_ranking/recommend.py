"""ORE-04: POST /recommend/pubkeys — merchants a point of view trusts.

The candidates are the authors of kind-30402 product listings (NIP-99 / Open
Markets), read from the search relay with the observer as lens: the relay
returns only listings from authors above the observer's trust floor, ordered
by that trust (`sort:rank`), and an optional `topic` narrows them to listings
whose text matches it. Ranks are the observer's GrapeRank influence, the same
number ORE-03 answers, so a merchant ranks the same on both endpoints.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession as AsyncDBSession

from app.core.database import get_db
from app.core.search_relay import SearchRelayUnavailable, fetch_events
from app.neo4j_db.driver import driver as neo4j_driver
from app.repos.user_repo import batch_influence_for_pubkeys
from app.routers.open_ranking.availability import ensure_pov_available
from app.routers.open_ranking.capabilities import resolve_algorithm
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

# Characters that would let a topic smuggle its own NIP-50 extension token.
_TOKEN_SEPARATOR = ":"


def _topic_terms(topic: str | None) -> str:
    if topic is None:
        return ""
    if len(topic) > MAX_TOPIC_LENGTH:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail=f"topic exceeds the maximum length of {MAX_TOPIC_LENGTH}",
        )
    # Words carrying a `:` are dropped so a topic can never override the lens.
    words = [w for w in topic.split() if _TOKEN_SEPARATOR not in w]
    return " ".join(words)


async def trusted_merchants(observer: str, topic: str, want: int) -> list[str]:
    """Distinct listing authors in the relay's trust order, at most `want`."""
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
    merchants: list[str] = []
    seen: set[str] = set()
    for listing in listings:
        author = listing.get("pubkey")
        if isinstance(author, str) and author not in seen:
            seen.add(author)
            merchants.append(author)
            if len(merchants) == want:
                break
    return merchants


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
    topic = _topic_terms(req.topic)

    algo_id, observer = resolve_algorithm(
        ENDPOINT, req.algorithm, pov, forced_observer=signer
    )
    await ensure_pov_available(db, ENDPOINT, algo_id, observer)

    try:
        merchants = await trusted_merchants(observer, topic, limit)
    except SearchRelayUnavailable as e:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=str(e)
        ) from e

    async with neo4j_driver.session() as session:
        influence = await batch_influence_for_pubkeys(session, merchants, observer)

    # The relay's lens already dropped sellers below the floor; a zero here is
    # a seller the graph has no score for, which "trusted" cannot claim.
    results = [
        RankResult(pubkey=pk, rank=rank)
        for pk in merchants
        if (rank := _safe_rank(influence.get(pk))) > 0
    ]
    results.sort(key=lambda r: (-r.rank, r.pubkey))
    return RecommendPubkeysResponse(results=results[:limit], ttl=TTL_HINTS[ENDPOINT])
