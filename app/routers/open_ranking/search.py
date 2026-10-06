"""ORE-05: POST /search/pubkeys."""

from __future__ import annotations

import re

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession as AsyncDBSession

from app.core.database import get_db
from app.core.search_relay import SearchRelayUnavailable, ore_search
from app.routers.open_ranking.availability import (
    ensure_pov_available,
    ensure_search_lens_serves_pov,
)
from app.routers.open_ranking.capabilities import resolve_algorithm
from app.routers.open_ranking.common import (
    MAX_QUERY_LEN,
    TTL_HINTS,
    validate_positive_limit,
    validate_pubkey,
)
from app.routers.open_ranking.schemas import (
    RankResult,
    SearchPubkeysRequest,
    SearchPubkeysResponse,
    ore_responses,
)
from app.utils.auth.nwt import optional_nwt_signer

router = APIRouter()


DEFAULT_LIMIT = 100  # provider default per ORE-05 §"limit"
MAX_LIMIT = 400

# Strip control characters; keep printable text only.
_SANITIZE_RE = re.compile(r"[\x00-\x1f\x7f]")


@router.post(
    "/search/pubkeys",
    response_model=SearchPubkeysResponse,
    summary="ORE-05: search Nostr profiles by free-form text",
    responses=ore_responses(),
)
async def search_pubkeys(
    req: SearchPubkeysRequest,
    signer: str | None = Depends(optional_nwt_signer),
    db: AsyncDBSession = Depends(get_db),
) -> SearchPubkeysResponse:
    sanitized = _SANITIZE_RE.sub("", req.query).strip()
    if not sanitized:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail="query field is missing or empty",
        )
    if len(req.query) > MAX_QUERY_LEN:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail=f"query exceeds the maximum length of {MAX_QUERY_LEN} characters",
        )

    pov = validate_pubkey(req.pov, "pov") if req.pov else None
    limit = validate_positive_limit(req.limit, max_value=MAX_LIMIT) or DEFAULT_LIMIT

    algo_id, observer = resolve_algorithm(
        "/search/pubkeys", req.algorithm, pov, forced_observer=signer
    )
    await ensure_pov_available(db, "/search/pubkeys", algo_id, observer)

    # rank is the relay's own search relevance, in the relay's order: the
    # number that orders the results, never a server-side estimate.
    try:
        found = await ore_search(sanitized, observer, limit)
    except SearchRelayUnavailable as e:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=str(e)
        ) from e
    await ensure_search_lens_serves_pov(db, algo_id, observer, found.lens)

    # The relay caps hits at the limit it was passed.
    results = [RankResult(pubkey=h.pubkey, rank=h.score) for h in found.hits]

    return SearchPubkeysResponse(
        results=results,
        ttl=TTL_HINTS["/search/pubkeys"],
    )
