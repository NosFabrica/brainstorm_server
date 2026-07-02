"""ORE-05: POST /search/pubkeys."""

from __future__ import annotations

import math
import re

from fastapi import APIRouter, Depends, HTTPException, status

from app.core.vespa import search as vespa_search
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
)
from app.utils.auth.nwt import optional_nwt_signer


router = APIRouter()


DEFAULT_LIMIT = 100  # provider default per ORE-05 §"limit"
MAX_LIMIT = 400  # mirrors the existing /search/byText cap

# Strip control characters; keep printable text only.
_SANITIZE_RE = re.compile(r"[\x00-\x1f\x7f]")


def _safe_rank(value) -> float:
    if value is None:
        return 0.0
    try:
        f = float(value)
    except (TypeError, ValueError):
        return 0.0
    if math.isnan(f) or math.isinf(f):
        return 0.0
    return f


@router.post(
    "/search/pubkeys",
    response_model=SearchPubkeysResponse,
    summary="ORE-05: search Nostr profiles by free-form text",
)
async def search_pubkeys(
    req: SearchPubkeysRequest,
    signer: str | None = Depends(optional_nwt_signer),
) -> SearchPubkeysResponse:
    if not isinstance(req.query, str) or not req.query.strip():
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

    _algo_id, observer = resolve_algorithm(
        "/search/pubkeys", req.algorithm, pov, forced_observer=signer
    )
    sanitized = _SANITIZE_RE.sub("", req.query).strip()

    # Vespa returns documents with `_quality_score` (the observer's score for
    # the matched profile). That score IS the rank for ORE-05.
    hits = await vespa_search(
        query_text=sanitized,
        user_pubkey=observer,
        hits=limit,
        include_zero_score_results=True,
    )

    results: list[RankResult] = []
    for h in hits:
        pk = h.get("pubkey")
        if not isinstance(pk, str):
            continue
        results.append(
            RankResult(pubkey=pk, rank=_safe_rank(h.get("_quality_score")))
        )

    return SearchPubkeysResponse(
        results=results,
        ttl=TTL_HINTS["/search/pubkeys"],
    )
