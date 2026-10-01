"""Follow-graph queries that aren't tied to the /user resource domain.

The single-pair Path network lookup behind GET /shortestPath (ADR 0004).
"""

from typing import NamedTuple

from fastapi import HTTPException, status

from app.core.loggr import loggr
from app.neo4j_db.driver import driver as neo4j_driver
from app.repos.user_repo import ShortestPathTimeout, get_all_shortest_follow_paths
from app.schemas.request_response_schemas import ShortestPathData
from app.utils.nostr import resolve_pubkey_or_400

logger = loggr.get_logger(__name__)


class PathNetwork(NamedTuple):
    layers: list[list[str]]
    links: list[list[list[int]]]


def _path_network(paths: list[list[str]], hops: int) -> PathNetwork:
    layers = [sorted({path[i] for path in paths}) for i in range(1, hops)]
    position = [{pk: j for j, pk in enumerate(layer)} for layer in layers]
    links = []
    for i in range(len(layers) - 1):
        targets: list[set[int]] = [set() for _ in layers[i]]
        for path in paths:
            targets[position[i][path[i + 1]]].add(position[i + 1][path[i + 2]])
        links.append([sorted(t) for t in targets])
    return PathNetwork(layers, links)


async def get_shortest_follow_path(
    from_raw: str,
    to_raw: str,
    max_hops: int,
) -> ShortestPathData:
    from_hex = resolve_pubkey_or_400(from_raw, "from")
    to_hex = resolve_pubkey_or_400(to_raw, "to")

    # Anyone is zero hops from themselves — answered without touching the
    # graph. Also mandatory: Neo4j rejects same-node shortest-path queries.
    if from_hex == to_hex:
        return ShortestPathData(
            from_pubkey=from_hex,
            to_pubkey=to_hex,
            reachable=True,
            hops=0,
            path_count=1,
            layers=[],
            links=[],
            max_hops=max_hops,
        )

    try:
        async with neo4j_driver.session() as session:
            paths = await get_all_shortest_follow_paths(
                session, from_hex, to_hex, max_hops
            )
    except ShortestPathTimeout:
        logger.warning("shortestPath timeout from=%s to=%s", from_hex, to_hex)
        raise HTTPException(
            status_code=status.HTTP_504_GATEWAY_TIMEOUT,
            detail="This path network is too large to compute right now.",
        )

    if not paths:
        logger.info("shortestPath unreachable max_hops=%d", max_hops)
        return ShortestPathData(
            from_pubkey=from_hex,
            to_pubkey=to_hex,
            reachable=False,
            hops=None,
            path_count=0,
            layers=[],
            links=[],
            max_hops=max_hops,
        )

    hops = len(paths[0]) - 1
    network = _path_network(paths, hops)
    logger.info(
        "shortestPath hops=%d paths=%d connectors=%d links=%d",
        hops,
        len(paths),
        sum(len(layer) for layer in network.layers),
        sum(len(t) for layer in network.links for t in layer),
    )
    return ShortestPathData(
        from_pubkey=from_hex,
        to_pubkey=to_hex,
        reachable=True,
        hops=hops,
        path_count=len(paths),
        layers=network.layers,
        links=network.links,
        max_hops=max_hops,
    )
