import asyncio
from datetime import datetime, timedelta
from app.utils.rate_limiting.rate_limiting import validateIfRequestedTooOftenByIP
from fastapi import HTTPException
from fastapi import APIRouter, Depends, Query, Request, status
from app.core.database import get_db
from app.schemas.request_body_schemas import SubmitNostrAuthChallengeBody
from app.schemas.request_response_schemas import (
    GetOwnLatestGraperankResponse,
    GetOwnUserDataResponse,
    GetUserConnectionsResponse,
    GetUserDataResponse,
    GetUserOverviewResponse,
    GetUserStatsResponse,
    PublishAssistantProfileData,
    PublishAssistantProfileResponse,
)
from app.core.config import settings

from sqlalchemy.ext.asyncio import AsyncSession as AsyncDBSession
from app.schemas.schemas import OwnUserData
from app.services.assistant_profile_service import publish_assistant_kind0_for_user
from app.services.brainstorm_request_service import create_brainstorm_request
from app.services.user_service import (
    DEFAULT_PAGE_SIZE,
    DEFAULT_VERIFIED_THRESHOLD,
    MAX_PAGE_SIZE,
    TIER_HIGH,
    TIER_NEUTRAL,
    TIER_TRUSTED,
    ConnectionKind,
    get_own_latest_graperank,
    get_user_connections,
    get_user_graph_data,
    get_user_history_data,
    get_user_overview,
    get_user_stats,
)
from app.utils.auth.auth_models import JWTData

CHALLENGE_TTL = 120  # seconds (2 minutes)

router = APIRouter()


@router.get(
    path="/graperankResult",
    tags=[],
    dependencies=[],
    summary="Get own graperank result endpoint (latest result)",
)
async def get_own_latest_graperank_endpoint(
    request: Request,
    db: AsyncDBSession = Depends(dependency=get_db),
) -> GetOwnLatestGraperankResponse:

    jwt_data: JWTData = request.state.jwt_data
    user_pubkey = jwt_data.nostr_pubkey

    result = await get_own_latest_graperank(db, user_pubkey)

    return GetOwnLatestGraperankResponse(data=result)


@router.post(
    path="/graperank",
    tags=[],
    dependencies=[],
    summary="Start a graperank calculation",
)
async def create_graperank_calc_endpoint(
    request: Request,
    db: AsyncDBSession = Depends(dependency=get_db),
) -> GetOwnLatestGraperankResponse:

    jwt_data: JWTData = request.state.jwt_data
    user_pubkey = jwt_data.nostr_pubkey

    if request.client:
        await validateIfRequestedTooOftenByIP(request.client.host)

    if settings.block_frequent_graperank_requests:

        latest = await get_own_latest_graperank(db, user_pubkey)

        if latest and latest.created_at.replace(
            tzinfo=None
        ) > datetime.now() - timedelta(
            minutes=settings.block_frequent_graperank_requests_minutes
        ):
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="The last triggered Graperank was too recent",
            )

    result = await create_brainstorm_request(
        db=db,
        algorithm="graperank",
        parameters=user_pubkey,
        pubkey=user_pubkey,
        nsec_exists=True,
    )

    return GetOwnLatestGraperankResponse(data=result)


@router.get(
    path="/self",
    tags=[],
    dependencies=[],
    summary="Get own user data endpoint",
)
async def get_own_user_data_endpoint(
    request: Request,
    db: AsyncDBSession = Depends(dependency=get_db),
) -> GetOwnUserDataResponse:

    jwt_data: JWTData = request.state.jwt_data
    user_pubkey = jwt_data.nostr_pubkey

    result, history = await asyncio.gather(
        get_user_graph_data(user_pubkey),
        get_user_history_data(db, user_pubkey),
    )

    return GetOwnUserDataResponse(data=OwnUserData(graph=result, history=history))


@router.post(
    path="/assistantProfile",
    tags=[],
    dependencies=[],
    summary="Publish a kind 0 metadata event for the user's brainstorm assistant",
)
async def publish_assistant_profile_endpoint(
    request: Request,
    db: AsyncDBSession = Depends(dependency=get_db),
) -> PublishAssistantProfileResponse:

    jwt_data: JWTData = request.state.jwt_data
    user_pubkey = jwt_data.nostr_pubkey

    event_id, assistant_pubkey = await publish_assistant_kind0_for_user(
        db, user_pubkey
    )

    return PublishAssistantProfileResponse(
        data=PublishAssistantProfileData(
            event_id=event_id,
            assistant_pubkey=assistant_pubkey,
        )
    )


@router.get(
    path="/{pubkey}/overview",
    summary="Lightweight overview: influence + relationship counts",
)
async def get_user_overview_endpoint(
    request: Request,
    pubkey: str,
) -> GetUserOverviewResponse:
    jwt_data: JWTData = request.state.jwt_data
    result = await get_user_overview(pubkey=pubkey, observer=jwt_data.nostr_pubkey)
    return GetUserOverviewResponse(data=result)


@router.get(
    path="/{pubkey}/stats",
    summary="Per-section stats: total + verified + tier counts for all 6 relationships",
)
async def get_user_stats_endpoint(
    request: Request,
    pubkey: str,
    verified_threshold: float = Query(default=DEFAULT_VERIFIED_THRESHOLD, ge=0.0, le=1.0),
    tier_high: float = Query(default=TIER_HIGH, ge=0.0, le=1.0),
    tier_trusted: float = Query(default=TIER_TRUSTED, ge=0.0, le=1.0),
    tier_neutral: float = Query(default=TIER_NEUTRAL, ge=0.0, le=1.0),
) -> GetUserStatsResponse:
    jwt_data: JWTData = request.state.jwt_data
    result = await get_user_stats(
        pubkey=pubkey,
        observer=jwt_data.nostr_pubkey,
        verified_threshold=verified_threshold,
        tier_high=tier_high,
        tier_trusted=tier_trusted,
        tier_neutral=tier_neutral,
    )
    return GetUserStatsResponse(data=result)


@router.get(
    path="/{pubkey}/connections",
    summary="Paginated connection list for a given kind",
)
async def get_user_connections_endpoint(
    request: Request,
    pubkey: str,
    kind: ConnectionKind = Query(..., description="Which relationship list to fetch"),
    limit: int = Query(default=DEFAULT_PAGE_SIZE, ge=1, le=MAX_PAGE_SIZE),
    cursor: str | None = Query(default=None),
) -> GetUserConnectionsResponse:
    jwt_data: JWTData = request.state.jwt_data
    result = await get_user_connections(
        pubkey=pubkey,
        observer=jwt_data.nostr_pubkey,
        kind=kind,
        limit=limit,
        cursor=cursor,
    )
    return GetUserConnectionsResponse(data=result)


@router.get(
    path="/{pubkey}",
    tags=[],
    dependencies=[],
    summary="Get user by pubkey data endpoint",
)
async def get_user_by_pubkey_data_endpoint(
    request: Request, pubkey: str
) -> GetUserDataResponse:

    jwt_data: JWTData = request.state.jwt_data
    user_pubkey = jwt_data.nostr_pubkey

    result = await get_user_graph_data(pubkey, user_pubkey)

    return GetUserDataResponse(data=result)
