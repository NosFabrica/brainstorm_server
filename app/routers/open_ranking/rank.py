"""ORE-03: POST /rank/pubkeys."""

from __future__ import annotations

import math

from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession as AsyncDBSession

from app.core.database import get_db
from app.neo4j_db.driver import driver as neo4j_driver
from app.repos.user_repo import batch_influence_for_pubkeys
from app.routers.open_ranking.availability import ensure_pov_available
from app.routers.open_ranking.capabilities import resolve_algorithm
from app.routers.open_ranking.common import (
    TTL_HINTS,
    enforce_batch_size,
    validate_positive_limit,
    validate_pubkey,
    validate_pubkey_list,
)
from app.routers.open_ranking.schemas import (
    RankPubkeysRequest,
    RankPubkeysResponse,
    RankResult,
)
from app.utils.auth.nwt import optional_nwt_signer


router = APIRouter()


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
    "/rank/pubkeys",
    response_model=RankPubkeysResponse,
    summary="ORE-03: rank a batch of pubkeys",
)
async def rank_pubkeys(
    req: RankPubkeysRequest,
    signer: str | None = Depends(optional_nwt_signer),
    db: AsyncDBSession = Depends(get_db),
) -> RankPubkeysResponse:
    # 413 must fire BEFORE per-element validation (avoid 1000+ regex passes
    # for an oversize payload).
    if isinstance(req.pubkeys, list):
        enforce_batch_size(len(req.pubkeys))

    pubkeys = validate_pubkey_list(req.pubkeys, "pubkeys")
    pov = validate_pubkey(req.pov, "pov") if req.pov else None
    limit = validate_positive_limit(req.limit)

    algo_id, observer = resolve_algorithm(
        "/rank/pubkeys", req.algorithm, pov, forced_observer=signer
    )
    await ensure_pov_available(db, "/rank/pubkeys", algo_id, observer)

    # Default limit = number of pubkeys. Silently clamp larger limits down
    # (per ORE-03 §"limit").
    effective_limit = limit if limit is not None else len(pubkeys)
    effective_limit = min(effective_limit, len(pubkeys))

    async with neo4j_driver.session() as session:
        influence_map = await batch_influence_for_pubkeys(
            session, pubkeys, observer
        )

    # Build results: 0.0 for unknown / missing; sort DESC by rank then
    # deterministic by pubkey for stable output across calls.
    ranked = [
        RankResult(pubkey=pk, rank=_safe_rank(influence_map.get(pk)))
        for pk in pubkeys
    ]
    ranked.sort(key=lambda r: (-r.rank, r.pubkey))
    ranked = ranked[:effective_limit]

    return RankPubkeysResponse(results=ranked, ttl=TTL_HINTS["/rank/pubkeys"])
