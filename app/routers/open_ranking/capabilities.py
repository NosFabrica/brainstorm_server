"""Open Ranking capability document (ORE-01) and algorithm-resolution logic.

The capability doc is intentionally hand-written here rather than computed: it
is the public protocol surface this provider commits to and changing it is a
breaking change for clients.
"""

from __future__ import annotations

from fastapi import HTTPException, status

from app.utils.observer import default_observer_pubkey

# Endpoint -> ordered list of supported Algorithm Objects. The first element of
# each list is the default algorithm for that endpoint (ORE-01 §"Algorithm
# selection").
CAPABILITY_DOC: dict[str, list[dict]] = {
    "/stats/pubkey": [
        {
            "id": "graperank",
            "name": "GrapeRank (global)",
            "description": (
                "GrapeRank from the provider's default observer. "
                "Higher is more reputable."
            ),
        },
        {
            "id": "graperank-pov",
            "name": "GrapeRank (personalized)",
            "description": "GrapeRank computed from the provided pov pubkey.",
            "pov": True,
        },
    ],
    "/rank/pubkeys": [
        {"id": "graperank", "name": "GrapeRank (global)"},
        {"id": "graperank-pov", "name": "GrapeRank (personalized)", "pov": True},
    ],
    "/search/pubkeys": [
        {
            "id": "relevance",
            "name": "Relevance (global)",
            "description": (
                "kind 0 profile text-matching score multiplied by a "
                "web-of-trust metric (default perspective)"
            ),
        },
        {
            "id": "relevance-pov",
            "name": "Relevance (personalized)",
            "description": (
                "kind 0 profile text-matching score multiplied by a "
                "web-of-trust metric (personalized)"
            ),
            "pov": True,
        },
    ],
    "/followers": [
        {"id": "graperank", "name": "GrapeRank (global)"},
        {"id": "graperank-pov", "name": "GrapeRank (personalized)", "pov": True},
    ],
    "/muters": [
        {"id": "graperank", "name": "GrapeRank (global)"},
        {"id": "graperank-pov", "name": "GrapeRank (personalized)", "pov": True},
    ],
}


# Algorithm ids that require a pov pubkey on the request. Kept in sync with the
# CAPABILITY_DOC above — single source of truth for "is this personalized?".
def _pov_required_ids() -> set[str]:
    return {
        algo["id"]
        for algos in CAPABILITY_DOC.values()
        for algo in algos
        if algo.get("pov")
    }


_POV_REQUIRED: set[str] = _pov_required_ids()


def algorithm_requires_pov(algo_id: str) -> bool:
    """Whether this algorithm id is personalized (declares `pov: true` in the
    capability document)."""
    return algo_id in _POV_REQUIRED


def resolve_algorithm(
    endpoint: str,
    requested: str | None,
    pov: str | None,
    forced_observer: str | None = None,
) -> tuple[str, str]:
    """Apply ORE-01 algorithm-selection rules and return
    `(algorithm_id, observer_pubkey)`.

    - If `requested` is None, use the endpoint's default (first list element).
    - If `requested` is not supported by the endpoint, raise 422.
    - If the chosen algorithm requires a pov and none was provided, raise 422.
    - For global algorithms, the observer is the provider's default observer.

    `forced_observer` is set when the provider runs in authenticated mode
    (`settings.open_ranking_require_auth`): the observer is ALWAYS the
    authenticated signer, so `pov` is ignored and a pov-requiring algorithm no
    longer needs an explicit `pov`. The algorithm id is still validated so an
    unsupported `algorithm` keeps returning 422.

    Errors are signalled per the ORE error tables (HTTP 422 with an X-Reason
    header attached upstream by the router layer).
    """
    algos = CAPABILITY_DOC.get(endpoint)
    if algos is None:
        # Programmer error rather than client error — endpoint isn't advertised
        # in CAPABILITY_DOC but a handler called us anyway.
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Endpoint {endpoint} not in capability document",
        )

    algo: dict | None
    if requested is None:
        algo = algos[0]
    else:
        algo = next((a for a in algos if a["id"] == requested), None)
        if algo is None:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
                detail=(f"Algorithm '{requested}' is not supported by {endpoint}"),
            )

    # Authenticated mode: the signer's own perspective overrides everything.
    # pov is ignored and pov-requiring algorithms don't need an explicit pov.
    if forced_observer is not None:
        return algo["id"], forced_observer

    if algo.get("pov"):
        if not pov:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
                detail=(f"Algorithm '{algo['id']}' requires a 'pov' pubkey"),
            )
        return algo["id"], pov

    # Global algorithm — pov MUST be ignored per ORE-01.
    return algo["id"], default_observer_pubkey()
