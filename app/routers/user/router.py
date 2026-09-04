import asyncio
from datetime import datetime, timedelta
from typing import Optional
from app.utils.rate_limiting.rate_limiting import validateIfRequestedTooOftenByIP
from fastapi import HTTPException
from fastapi import APIRouter, Depends, Query, Request, status
from app.core.database import get_db
from app.schemas.request_body_schemas import (
    RefreshSubscriptionBody,
    SubmitFollowListBody,
)
from app.schemas.request_response_schemas import (
    ErrorResponseSchema,
    GetOwnLatestGraperankResponse,
    GetSubscriptionResponse,
    GetOwnUserDataResponse,
    GetUserConnectionsResponse,
    GetUserDataResponse,
    GetUserHistoryResponse,
    GetUserOverviewResponse,
    GetUserStatsResponse,
    IsSearchObserverResponse,
    PublishAssistantProfileData,
    PublishAssistantProfileResponse,
    RefreshSubscriptionResponse,
    SubmitFollowListResponse,
)
from app.repos.brainstorm_nsec import (
    get_is_observer_search_available_by_pubkey_on_db,
    update_assistant_kind0_published_at_on_db,
)
from app.core.config import settings

from sqlalchemy.ext.asyncio import AsyncSession as AsyncDBSession
from app.schemas.schemas import FollowListIngestResult, OwnUserData
from app.services.assistant_profile_service import publish_assistant_kind0_for_user
from app.services.onboarding_service import ingest_follow_list
from app.services.brainstorm_request_service import create_brainstorm_request
from app.services.manual_quota import enforce_manual_quota
from app.services.user_service import (
    DEFAULT_PAGE_SIZE,
    MAX_PAGE_SIZE,
    ConnectionKind,
    get_own_latest_graperank,
    get_user_connections,
    get_user_graph_data,
    get_user_history_data,
    get_user_overview,
    get_user_stats,
)
from app.core.flash import FlashCredentialError, FlashUnavailable
from app.services.billing_service import apply_entitlement
from app.services.subscription_view_service import (
    read_refreshed_subscription_view,
    read_subscription_view,
)
from app.utils.rate_limiting.rate_limiting import (
    validate_subscription_refresh_allowed,
)
from app.services.verified_cutoffs import VerifiedCutoffs
from app.routers.user.dependencies import get_verified_cutoffs, resolve_observer
from app.utils.auth.auth_models import JWTData
from app.utils.api_validators import verify_token_optional

CHALLENGE_TTL = 120  # seconds (2 minutes)

router = APIRouter()

# Public endpoints (optional auth): looking up a user by pubkey is browsable
# anonymously. When authenticated, the caller's pubkey is used as the observer
# perspective; otherwise we fall back to the default observer.
public_router = APIRouter()


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

    await enforce_manual_quota(db, user_pubkey)

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


@router.post(
    path="/followList",
    tags=[],
    dependencies=[],
    summary="Ingest a freshly-signed onboarding follow list synchronously",
    responses={
        status.HTTP_400_BAD_REQUEST: {
            "model": ErrorResponseSchema,
            "description": "Event is not a kind-3 follow list, or is unparseable.",
        },
        status.HTTP_401_UNAUTHORIZED: {
            "model": ErrorResponseSchema,
            "description": "Event signature is invalid.",
        },
        status.HTTP_403_FORBIDDEN: {
            "model": ErrorResponseSchema,
            "description": "Event author does not match the authenticated user.",
        },
        status.HTTP_429_TOO_MANY_REQUESTS: {
            "model": ErrorResponseSchema,
            "description": "Rate limit exceeded for the caller's IP.",
        },
    },
)
async def submit_follow_list_endpoint(
    request: Request,
    body: SubmitFollowListBody,
) -> SubmitFollowListResponse:
    jwt_data: JWTData = request.state.jwt_data
    user_pubkey = jwt_data.nostr_pubkey

    if request.client:
        await validateIfRequestedTooOftenByIP(request.client.host)

    follow_count = await ingest_follow_list(
        user_pubkey, body.signed_event.model_dump()
    )

    return SubmitFollowListResponse(
        data=FollowListIngestResult(followCount=follow_count)
    )


@router.get(
    path="/history",
    tags=[],
    dependencies=[],
    summary="Get own user history (graperank trigger/calculation timestamps, ta_pubkey)",
)
async def get_own_user_history_endpoint(
    request: Request,
    db: AsyncDBSession = Depends(dependency=get_db),
) -> GetUserHistoryResponse:
    jwt_data: JWTData = request.state.jwt_data
    history = await get_user_history_data(db, jwt_data.nostr_pubkey)
    return GetUserHistoryResponse(data=history)


@router.get(
    path="/self",
    tags=[],
    dependencies=[],
    summary=(
        "Get own user data endpoint. DEPRECATED: New callers should use "
        "/user/{pubkey}/overview, /user/history, /user/{pubkey}/stats, "
        "and /user/{pubkey}/connections instead."
    ),
    deprecated=True,
)
async def get_own_user_data_endpoint(
    request: Request,
    db: AsyncDBSession = Depends(dependency=get_db),
) -> GetOwnUserDataResponse:
    jwt_data: JWTData = request.state.jwt_data
    user_pubkey = jwt_data.nostr_pubkey

    graph, history = await asyncio.gather(
        get_user_graph_data(user_pubkey, user_pubkey),
        get_user_history_data(db, user_pubkey),
    )

    return GetOwnUserDataResponse(data=OwnUserData(graph=graph, history=history))


@router.get(
    path="/isSearchObserver",
    tags=[],
    dependencies=[],
    summary="Whether the caller's pubkey is searchable as an observer on Vespa",
)
async def is_search_observer_endpoint(
    request: Request,
    db: AsyncDBSession = Depends(dependency=get_db),
) -> IsSearchObserverResponse:
    jwt_data: JWTData = request.state.jwt_data
    user_pubkey = jwt_data.nostr_pubkey

    is_available = await get_is_observer_search_available_by_pubkey_on_db(
        db, pubkey=user_pubkey
    )

    return IsSearchObserverResponse(data=is_available)


@router.get(
    path="/subscription",
    summary="The caller's subscription, as the UI displays it",
)
async def get_subscription_endpoint(
    request: Request,
    db: AsyncDBSession = Depends(dependency=get_db),
) -> GetSubscriptionResponse:
    # Always mounted, billing configured or not: an instance without Flash
    # answers the free default rather than 404ing every page load.
    jwt_data: JWTData = request.state.jwt_data
    view = await read_subscription_view(db, jwt_data.nostr_pubkey)
    return GetSubscriptionResponse(data=view)


@router.post(
    path="/subscription/refresh",
    summary="Re-read the caller's subscription from Flash and apply it",
)
async def refresh_subscription_endpoint(
    request: Request,
    body: RefreshSubscriptionBody | None = None,
    db: AsyncDBSession = Depends(dependency=get_db),
) -> RefreshSubscriptionResponse:
    # The guide's two return paths. Given the redirect's id we verify THAT
    # subscription; without one — a `pending` checkout, which Flash issues no id
    # for — we read by reference, its own instruction for that case.
    # `apply_entitlement` is what makes the id safe to take, and `verification`
    # is how the caller learns an id was refused rather than still confirming.
    jwt_data: JWTData = request.state.jwt_data
    pubkey = jwt_data.nostr_pubkey
    await validate_subscription_refresh_allowed(pubkey)

    subscription_id = body.subscription_id if body else None
    outcome = None
    if settings.flash_enabled:
        try:
            outcome = await apply_entitlement(
                db, external_ref=pubkey, subscription_id=subscription_id
            )
        except (FlashUnavailable, FlashCredentialError):
            # The caller wanted the current answer; an unreadable Flash means
            # the current answer is whatever we already hold.
            pass

    view = await read_refreshed_subscription_view(
        db, pubkey, outcome=outcome, id_given=subscription_id is not None
    )
    return RefreshSubscriptionResponse(data=view)


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

    event_id, assistant_pubkey = await publish_assistant_kind0_for_user(db, user_pubkey)
    # A manual publish also satisfies the upload task's first-run kind-0 check.
    await update_assistant_kind0_published_at_on_db(db, pubkey=user_pubkey)

    return PublishAssistantProfileResponse(
        data=PublishAssistantProfileData(
            event_id=event_id,
            assistant_pubkey=assistant_pubkey,
        )
    )


@public_router.get(
    path="/{pubkey}/overview",
    summary="Lightweight overview: influence + relationship counts + flagged_by_observer",
)
async def get_user_overview_endpoint(
    pubkey: str,
    jwt_data: Optional[JWTData] = Depends(verify_token_optional),
    cutoffs: VerifiedCutoffs = Depends(get_verified_cutoffs),
) -> GetUserOverviewResponse:
    result = await get_user_overview(
        pubkey=pubkey,
        observer=resolve_observer(jwt_data),
        verified_line=cutoffs.verified_line,
    )
    return GetUserOverviewResponse(data=result)


@public_router.get(
    path="/{pubkey}/stats",
    summary="Per-section stats: total + verified + tier counts for all 6 relationships",
)
async def get_user_stats_endpoint(
    pubkey: str,
    jwt_data: Optional[JWTData] = Depends(verify_token_optional),
    cutoffs: VerifiedCutoffs = Depends(get_verified_cutoffs),
) -> GetUserStatsResponse:
    result = await get_user_stats(
        pubkey=pubkey,
        observer=resolve_observer(jwt_data),
        cutoffs=cutoffs,
    )
    return GetUserStatsResponse(data=result)


@public_router.get(
    path="/{pubkey}/connections",
    summary="Paginated connection list for a given kind",
)
async def get_user_connections_endpoint(
    pubkey: str,
    kind: ConnectionKind = Query(..., description="Which relationship list to fetch"),
    limit: int = Query(default=DEFAULT_PAGE_SIZE, ge=1, le=MAX_PAGE_SIZE),
    cursor: str | None = Query(default=None),
    jwt_data: Optional[JWTData] = Depends(verify_token_optional),
    order: str = Query(default="desc", pattern="^(asc|desc)$"),
    tier: str
    | None = Query(
        default=None,
        pattern="^(high|medium_high|medium|medium_low|low|low_and_reported_by_2_or_more_trusted_pubkeys)$",
    ),
    verified_only: bool = Query(
        default=False,
        description=(
            "Keep only subjects verified for this `kind` under the observer's "
            "saved preset. Ignored for kind=flagged."
        ),
    ),
    with_total: bool = Query(
        default=False,
        description="Include the filtered total (extra graph scan); off by default",
    ),
    cutoffs: VerifiedCutoffs = Depends(get_verified_cutoffs),
) -> GetUserConnectionsResponse:
    result = await get_user_connections(
        pubkey=pubkey,
        observer=resolve_observer(jwt_data),
        cutoffs=cutoffs,
        kind=kind,
        limit=limit,
        cursor=cursor,
        order=order,
        tier=tier,
        verified_only=verified_only,
        with_total=with_total,
    )
    return GetUserConnectionsResponse(data=result)


@public_router.get(
    path="/{pubkey}",
    tags=[],
    dependencies=[],
    summary="Get user by pubkey data endpoint",
)
async def get_user_by_pubkey_data_endpoint(
    pubkey: str,
    jwt_data: Optional[JWTData] = Depends(verify_token_optional),
) -> GetUserDataResponse:
    result = await get_user_graph_data(pubkey, resolve_observer(jwt_data))

    return GetUserDataResponse(data=result)
