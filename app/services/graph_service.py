"""Follow-graph queries that aren't tied to the /user resource domain.

GET /shortestPath: the single-pair Path network, or Hops alone (ADR 0004).
"""

from typing import Awaitable, Callable, NamedTuple, TypeVar

from fastapi import HTTPException, status
from neo4j import AsyncSession as AsyncNeoSession

from app.core.loggr import loggr
from app.neo4j_db.driver import driver as neo4j_driver
from app.repos.user_repo import (
    ShortestPathTimeout,
    get_all_shortest_follow_paths,
    get_shortest_follow_hops,
)
from app.schemas.request_response_schemas import ShortestPathData
from app.utils.nostr import resolve_pubkey_or_400

logger = loggr.get_logger(__name__)

T = TypeVar("T")


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


def _no_network(
    from_hex: str, to_hex: str, max_hops: int, hops: int | None, path_count: int | None
) -> ShortestPathData:
    return ShortestPathData(
        from_pubkey=from_hex,
        to_pubkey=to_hex,
        reachable=hops is not None,
        hops=hops,
        path_count=path_count,
        layers=[],
        links=[],
        max_hops=max_hops,
    )


async def _query(
    from_hex: str,
    to_hex: str,
    max_hops: int,
    repo: Callable[[AsyncNeoSession, str, str, int], Awaitable[T]],
) -> T:
    try:
        async with neo4j_driver.session() as session:
            return await repo(session, from_hex, to_hex, max_hops)
    except ShortestPathTimeout:
        logger.warning("shortestPath timeout from=%s to=%s", from_hex, to_hex)
        raise HTTPException(
            status_code=status.HTTP_504_GATEWAY_TIMEOUT,
            detail="This path network is too large to compute right now.",
        )


def _resolve(from_raw: str, to_raw: str) -> tuple[str, str]:
    return resolve_pubkey_or_400(from_raw, "from"), resolve_pubkey_or_400(to_raw, "to")


async def get_shortest_follow_path(
    from_raw: str, to_raw: str, max_hops: int
) -> ShortestPathData:
    from_hex, to_hex = _resolve(from_raw, to_raw)
    # Zero hops from yourself, answered without the graph — Neo4j rejects
    # same-node shortest-path queries anyway.
    if from_hex == to_hex:
        return _no_network(from_hex, to_hex, max_hops, hops=0, path_count=1)

    paths = await _query(from_hex, to_hex, max_hops, get_all_shortest_follow_paths)
    if not paths:
        logger.info("shortestPath unreachable max_hops=%d", max_hops)
        return _no_network(from_hex, to_hex, max_hops, hops=None, path_count=0)

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


async def get_follow_hops(
    from_raw: str, to_raw: str, max_hops: int
) -> ShortestPathData:
    """Hops alone from one shortest path: `pathCount` null, no network."""
    from_hex, to_hex = _resolve(from_raw, to_raw)
    if from_hex == to_hex:
        return _no_network(from_hex, to_hex, max_hops, hops=0, path_count=None)
    hops = await _query(from_hex, to_hex, max_hops, get_shortest_follow_hops)
    return _no_network(from_hex, to_hex, max_hops, hops=hops, path_count=None)
