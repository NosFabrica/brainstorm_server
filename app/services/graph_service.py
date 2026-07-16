"""Follow-graph queries that aren't tied to the /user resource domain.

v1: the single-pair shortest-path lookup behind GET /shortestPath
(story shortest-path #1, ADR 0001).
"""

import random

from fastapi import HTTPException, status
from nostr_sdk import PublicKey

from app.neo4j_db.driver import driver as neo4j_driver
from app.repos.user_repo import get_all_shortest_follow_paths
from app.schemas.request_response_schemas import ShortestPathData


def _resolve_pubkey_or_400(value: str, param_name: str) -> str:
    """Hex or npub in, canonical hex out; anything unparseable is a 400."""
    try:
        return PublicKey.parse(value).to_hex()
    except Exception:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"{param_name} is not a valid hex pubkey or npub",
        )


async def get_shortest_follow_path(
    from_raw: str,
    to_raw: str,
    max_hops: int,
    max_paths: int,
) -> ShortestPathData:
    from_hex = _resolve_pubkey_or_400(from_raw, "from")
    to_hex = _resolve_pubkey_or_400(to_raw, "to")

    # Anyone is zero hops from themselves — answered without touching the
    # graph. Also mandatory: Neo4j rejects same-node shortest-path queries
    # (ADR 0001, Decision 1).
    if from_hex == to_hex:
        return ShortestPathData(
            from_pubkey=from_hex,
            to_pubkey=to_hex,
            reachable=True,
            hops=0,
            path=[from_hex],
            path_count=1,
            path_count_capped=False,
            max_hops=max_hops,
        )

    async with neo4j_driver.session() as session:
        paths, path_count = await get_all_shortest_follow_paths(
            session, from_hex, to_hex, max_hops, max_paths
        )

    if not paths:
        return ShortestPathData(
            from_pubkey=from_hex,
            to_pubkey=to_hex,
            reachable=False,
            hops=None,
            path=None,
            path_count=0,
            path_count_capped=False,
            max_hops=max_hops,
        )

    return ShortestPathData(
        from_pubkey=from_hex,
        to_pubkey=to_hex,
        reachable=True,
        hops=len(paths[0]) - 1,
        # Random pick over the (capped) materialized set — when the true count
        # exceeds max_paths this is a pick from the capped sample (documented
        # sampling bias, issue #43).
        path=random.choice(paths),
        path_count=min(path_count, max_paths),
        path_count_capped=path_count > max_paths,
        max_hops=max_hops,
    )
