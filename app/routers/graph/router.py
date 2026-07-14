"""Follow-graph endpoints not tied to the /user resource domain.

Mounted at root (see app/routers/router.py) so paths are exactly as specified
in issue #43 — a shortest path over a directed graph is a two-argument query,
and explicit `from`/`to` params keep the direction unambiguous.

Public read: the follow graph is public data and these are read-only
traversals, matching the auth posture of the /user/{pubkey}/* lookups.
"""

from fastapi import APIRouter, Query

from app.schemas.request_response_schemas import GetShortestPathResponse
from app.services import graph_service

router = APIRouter()


@router.get(
    path="/shortestPath",
    summary="Shortest directed FOLLOWS path(s) between two pubkeys",
)
async def get_shortest_path_endpoint(
    from_: str = Query(
        ...,
        alias="from",
        description=(
            "Source pubkey (hex or npub). Direction matters: paths follow "
            "FOLLOWS edges from here."
        ),
    ),
    to: str = Query(..., description="Target pubkey (hex or npub)."),
    maxHops: int = Query(
        default=30,
        ge=1,
        le=50,
        description="Traversal depth cap. Unreachable within this bound → reachable=false.",
    ),
    maxPaths: int = Query(
        default=1000,
        ge=1,
        le=1000,
        description="Cap on shortest paths materialized for counting / random selection.",
    ),
) -> GetShortestPathResponse:
    data = await graph_service.get_shortest_follow_path(from_, to, maxHops, maxPaths)
    return GetShortestPathResponse(data=data)
