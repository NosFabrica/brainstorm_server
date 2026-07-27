"""ORE-02: POST /stats/pubkey."""

from __future__ import annotations

import math

from fastapi import APIRouter, Depends

from app.routers.open_ranking.capabilities import resolve_algorithm
from app.routers.open_ranking.common import TTL_HINTS, validate_pubkey
from app.routers.open_ranking.schemas import (
    StatsPubkeyRequest,
    StatsPubkeyResponse,
)
from app.core.tier_thresholds import DEFAULT_VERIFIED_THRESHOLD
from app.services.user_service import get_user_overview
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
)
async def stats_pubkey(
    req: StatsPubkeyRequest,
    signer: str | None = Depends(optional_nwt_signer),
) -> StatsPubkeyResponse:
    pubkey = validate_pubkey(req.pubkey, "pubkey")
    pov = validate_pubkey(req.pov, "pov") if req.pov else None

    _algo_id, observer = resolve_algorithm(
        "/stats/pubkey", req.algorithm, pov, forced_observer=signer
    )

    # ORE-02 exposes only the rank and the raw counts, never the flagged fields,
    # so the verified line is unobservable here — pass the baseline.
    overview = await get_user_overview(
        pubkey=pubkey, observer=observer, verified_line=DEFAULT_VERIFIED_THRESHOLD
    )
    counts = overview.counts

    return StatsPubkeyResponse(
        pubkey=pubkey,
        rank=_safe_rank(overview.influence),
        follows=counts.following,
        followers=counts.followed_by,
        mutes=counts.muting,
        muters=counts.muted_by,
        reports=counts.reporting,
        reporters=counts.reported_by,
        # `first_seen_at` is not tracked yet — omit (the field is optional).
        ttl=TTL_HINTS["/stats/pubkey"],
    )
