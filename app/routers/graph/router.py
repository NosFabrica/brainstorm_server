"""Follow-graph endpoints not tied to the /user resource domain.

Mounted at root (see app/routers/router.py) so paths are exactly as specified
in issue #43 — a shortest path over a directed graph is a two-argument query,
and explicit `from`/`to` params keep the direction unambiguous.

Public read: the follow graph is public data and these are read-only
traversals, matching the auth posture of the /user/{pubkey}/* lookups.
"""

from typing import Literal

from fastapi import APIRouter, Query

from app.schemas.request_response_schemas import GetShortestPathResponse
from app.services import graph_service

router = APIRouter()


@router.get(
    path="/shortestPath",
    summary="Path network (or Hops alone) between two pubkeys over directed FOLLOWS",
    description=(
        "Returns the exact `pathCount` and every Connector on any shortest path, "
        "as `layers` (one per intermediate hop, sorted by pubkey; the two ends are "
        "left out) and `links` (`links[i][j]` = indexes into `layers[i+1]` that "
        "`layers[i][j]` follows; `from` follows all of the first layer and all of "
        "the last layer follows `to`). Deterministic and uncapped. With "
        "`only=hops`: `reachable`/`hops` only, `pathCount` null, empty "
        "`layers`/`links`. 504 when the graph query exceeds its time limit — "
        "never a partial network."
    ),
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
    only: Literal["hops"]
    | None = Query(
        default=None,
        description=(
            "`hops`: answer `reachable`/`hops` only, from a single shortest path. "
            "`pathCount` is null and `layers`/`links` are empty."
        ),
    ),
) -> GetShortestPathResponse:
    if only == "hops":
        data = await graph_service.get_follow_hops(from_, to, maxHops)
    else:
        data = await graph_service.get_shortest_follow_path(from_, to, maxHops)
    return GetShortestPathResponse(data=data)
