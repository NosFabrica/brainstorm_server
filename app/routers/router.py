import hashlib

from fastapi import Query
from fastapi import APIRouter, Depends, Request, Response

from app.core.database import get_db

from app.routers.admin.router import router as admin_router
from app.routers.auth_challenge.router import router as auth_challenge_router
from app.routers.graph.router import router as graph_router
from app.routers.graperank.router import router as graperank_router
from app.routers.network_alerts.router import router as network_alerts_router
from app.routers.nip50.router import router as nip50_router
from app.routers.open_ranking.router import router as open_ranking_router
from app.routers.search.router import router as search_router
from app.routers.setup.router import router as setup_router
from app.routers.user.router import router as user_router
from app.routers.user.router import public_router as public_user_router
from app.schemas.request_response_schemas import (
    GetWhitelistedPubkeysOfObserverResponse,
    WhitelistedPubkeys,
)
from app.repos.observer_whitelist_repo import (
    select_observer_whitelist_updated_at,
)
from app.services.user_service import (
    get_whitelisted_pubkeys_of_observer,
)
from app.utils.api_validators import verify_token
from sqlalchemy.ext.asyncio import AsyncSession as AsyncDBSession

router = APIRouter()

# Open Ranking provider (OREs 01/02/03/05/06/07). Mounted at root because the
# protocol mandates exact paths (e.g. /.well-known/open-ranking.json,
# /stats/pubkey, /rank/pubkeys, ...). Authentication is currently optional;
# ORE-A (NWT) wiring lives in a follow-up phase.
router.include_router(
    router=open_ranking_router,
    tags=["open-ranking"],
)

# Follow-graph queries (issue #43). Mounted at root so the path is exactly
# /shortestPath, decoupled from the /user resource domain.
router.include_router(
    router=graph_router,
    tags=["graph"],
)

# Network Alerts panel. Mounted at root alongside /shortestPath — an
# observer-parameterized question about the graph, not a /user sub-resource.
router.include_router(
    router=network_alerts_router,
    tags=["network-alerts"],
)

ADMIN_ROUTER_PREFIX = "/admin"

router.include_router(
    router=admin_router,
    prefix=ADMIN_ROUTER_PREFIX,
    tags=["admin"],
)

AUTH_CHALLENGE_ROUTER_PREFIX = "/authChallenge"

router.include_router(
    router=auth_challenge_router,
    prefix=AUTH_CHALLENGE_ROUTER_PREFIX,
    tags=["auth_challenge"],
)

SETUP_ROUTER_PREFIX = "/setup"

router.include_router(
    router=setup_router,
    prefix=SETUP_ROUTER_PREFIX,
    tags=["setup"],
)

SEARCH_ROUTER_PREFIX = "/search"

router.include_router(
    router=search_router,
    prefix=SEARCH_ROUTER_PREFIX,
    tags=["search"],
)

# NIP-50 search relay. Mounted at root because NIP-11 mandates the
# information document live at the same URL clients connect to over
# WebSocket — here that's ``/relay``.
router.include_router(
    router=nip50_router,
    tags=["nip50"],
)

USER_ROUTER_PREFIX = "/user"

router.include_router(
    dependencies=[Depends(verify_token)],
    router=user_router,
    prefix=USER_ROUTER_PREFIX,
    tags=["user"],
)

# Public, optional-auth /user/{pubkey}* lookups. Must be included AFTER the
# authenticated user_router so its static routes (e.g. /user/self) match before
# the "/{pubkey}" single-segment catch-all defined here.
router.include_router(
    router=public_user_router,
    prefix=USER_ROUTER_PREFIX,
    tags=["user"],
)

GRAPERANK_ROUTER_PREFIX = "/user/graperank"

router.include_router(
    dependencies=[Depends(verify_token)],
    router=graperank_router,
    prefix=GRAPERANK_ROUTER_PREFIX,
    tags=["user"],
)


@router.get(
    path="/whitelisted/{observer_pubkey}",
    tags=[],
    dependencies=[],
    summary="Get all the trusted pubkeys given the view of an observer",
)
async def get_whitelisted_pubkeys_of_observer_endpoint(
    request: Request,
    observer_pubkey: str,
    response: Response,
    # Lower bound == the graperank cutoff: the whitelist table only stores
    # above-cutoff observees, so the endpoint offers *more* selectivity, never
    # less. Asking below the cutoff is meaningless here.
    threshold: float = Query(default=0.02, ge=0.02, le=1.0),
    db: AsyncDBSession = Depends(dependency=get_db),
) -> GetWhitelistedPubkeysOfObserverResponse:

    # Cheap meta-read (no scores detoast) → ETag. Lets an unchanged whitelist
    # short-circuit to 304 before we scan/serialize the ~99k-key blob.
    updated_at = await select_observer_whitelist_updated_at(db, observer_pubkey)
    if updated_at is None:
        return GetWhitelistedPubkeysOfObserverResponse(
            data=WhitelistedPubkeys(
                observerPubkey=observer_pubkey, numPubkeys=0, pubkeys=[]
            )
        )

    etag = _whitelist_etag(observer_pubkey, threshold, updated_at.isoformat())
    if request.headers.get("if-none-match") == etag:
        return Response(
            status_code=304,
            headers={"ETag": etag, "Cache-Control": "private, no-cache"},
        )

    result = await get_whitelisted_pubkeys_of_observer(db, observer_pubkey, threshold)

    result_formated = WhitelistedPubkeys(
        observerPubkey=observer_pubkey, numPubkeys=len(result), pubkeys=result
    )

    response.headers["ETag"] = etag
    response.headers["Cache-Control"] = "private, no-cache"
    return GetWhitelistedPubkeysOfObserverResponse(data=result_formated)


def _whitelist_etag(observer_pubkey: str, threshold: float, updated_at: str) -> str:
    digest = hashlib.sha1(
        f"{observer_pubkey}:{threshold}:{updated_at}".encode()
    ).hexdigest()
    return f'"{digest}"'
