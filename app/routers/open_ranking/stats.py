"""ORE-02: POST /stats/pubkey."""

from __future__ import annotations

import math

from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession as AsyncDBSession

from app.core.database import get_db
from app.routers.open_ranking.availability import ensure_pov_available
from app.routers.open_ranking.capabilities import resolve_algorithm
from app.routers.open_ranking.common import TTL_HINTS, validate_pubkey
from app.routers.open_ranking.schemas import (
    StatsPubkeyRequest,
    StatsPubkeyResponse,
    ore_responses,
)
from app.services.user_service import get_user_rank_and_counts
from app.utils.auth.nwt import optional_nwt_signer


router = APIRouter()


def _safe_rank(value) -> float:
    """Coerce Neo4j influence to a JSON-safe number. Unknown / null / non-
    finite values become 0.0 — semantically "no observed trust" for the
    GrapeRank family of algorithms.
    """
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
    "/stats/pubkey",
    response_model=StatsPubkeyResponse,
    summary="ORE-02: rank + stats for a single pubkey",
    responses=ore_responses(),
)
async def stats_pubkey(
    req: StatsPubkeyRequest,
    signer: str | None = Depends(optional_nwt_signer),
    db: AsyncDBSession = Depends(get_db),
) -> StatsPubkeyResponse:
    pubkey = validate_pubkey(req.pubkey, "pubkey")
    pov = validate_pubkey(req.pov, "pov") if req.pov else None

    algo_id, observer = resolve_algorithm(
        "/stats/pubkey", req.algorithm, pov, forced_observer=signer
    )
    await ensure_pov_available(db, "/stats/pubkey", algo_id, observer)

    # NOTE: this endpoint does NOT apply the observer's saved GrapeRank preset.
    # It returns `rank` (raw influence_<observer>) and raw counts, none of which
    # move with a preset — so there's no line to resolve, hence no Postgres read
    # and no line to get wrong. The observer itself still applies: it picks whose
    # web of trust the rank comes from (signer / pov / house key).
    # Surfacing verified / tier / flagged means switching to `get_user_overview`
    # + `resolve_verified_cutoffs`, as the /user reads do.
    result = await get_user_rank_and_counts(pubkey=pubkey, observer=observer)
    counts = result.counts

    return StatsPubkeyResponse(
        pubkey=pubkey,
        rank=_safe_rank(result.influence),
        follows=counts.following,
        followers=counts.followed_by,
        mutes=counts.muting,
        muters=counts.muted_by,
        reports=counts.reporting,
        reporters=counts.reported_by,
        # `first_seen_at` is not tracked yet — omit (the field is optional).
        ttl=TTL_HINTS["/stats/pubkey"],
    )
