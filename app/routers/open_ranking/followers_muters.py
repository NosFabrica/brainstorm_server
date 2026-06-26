"""ORE-06 (POST /followers) and ORE-07 (POST /muters).

Both endpoints return the top-ranked inbound connections (FOLLOWS or MUTES
edges) for a target pubkey, sorted by `influence_<observer>` DESC.
"""

from __future__ import annotations

import asyncio
import math

from fastapi import APIRouter

from app.core.redis_db import redis_client
from app.message_queue_tasks.process_strfry_event import (
    FOLLOWED_BY_KEY_PREFIX,
    MUTED_BY_KEY_PREFIX,
)
from app.neo4j_db.driver import driver as neo4j_driver
from app.repos.user_repo import get_top_inbound_by_influence
from app.routers.open_ranking.capabilities import resolve_algorithm
from app.routers.open_ranking.common import (
    TTL_HINTS,
    validate_positive_limit,
    validate_pubkey,
)
from app.routers.open_ranking.schemas import (
    FollowersOrMutersRequest,
    FollowersOrMutersResponse,
    RankResult,
)


router = APIRouter()


DEFAULT_LIMIT = 100  # sensible default per ORE-06/07


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


async def _redis_inbound_count(prefix: str, pubkey: str) -> int:
    return int(await redis_client.scard(f"{prefix}{pubkey}"))


async def _top_inbound_response(
    endpoint: str,
    relation: str,
    redis_prefix: str,
    req: FollowersOrMutersRequest,
) -> FollowersOrMutersResponse:
    pubkey = validate_pubkey(req.pubkey, "pubkey")
    pov = validate_pubkey(req.pov, "pov") if req.pov else None
    limit = validate_positive_limit(req.limit) or DEFAULT_LIMIT

    _algo_id, observer = resolve_algorithm(endpoint, req.algorithm, pov)

    # Fire the graph query and the Redis total count in parallel.
    async def _fetch_top() -> list:
        async with neo4j_driver.session() as session:
            return await get_top_inbound_by_influence(
                session, pubkey, observer, relation, limit
            )

    top, total = await asyncio.gather(
        _fetch_top(),
        _redis_inbound_count(redis_prefix, pubkey),
    )

    results = [
        RankResult(pubkey=conn.pubkey, rank=_safe_rank(conn.influence))
        for conn in top
    ]

    return FollowersOrMutersResponse(
        results=results,
        total=total,
        ttl=TTL_HINTS[endpoint],
    )


@router.post(
    "/followers",
    response_model=FollowersOrMutersResponse,
    summary="ORE-06: top-ranked followers of a pubkey",
)
async def followers(req: FollowersOrMutersRequest) -> FollowersOrMutersResponse:
    return await _top_inbound_response(
        endpoint="/followers",
        relation="FOLLOWS",
        redis_prefix=FOLLOWED_BY_KEY_PREFIX,
        req=req,
    )


@router.post(
    "/muters",
    response_model=FollowersOrMutersResponse,
    summary="ORE-07: top-ranked muters of a pubkey",
)
async def muters(req: FollowersOrMutersRequest) -> FollowersOrMutersResponse:
    return await _top_inbound_response(
        endpoint="/muters",
        relation="MUTES",
        redis_prefix=MUTED_BY_KEY_PREFIX,
        req=req,
    )
