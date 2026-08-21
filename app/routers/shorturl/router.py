from fastapi import APIRouter, Depends, Request
from sqlalchemy.ext.asyncio import AsyncSession as AsyncDBSession

from app.core.database import get_db
from app.schemas.request_body_schemas import CreateShortUrlBody
from app.schemas.request_response_schemas import (
    CreateShortUrlResponse,
    GetShortUrlResponse,
)
from app.schemas.schemas import CreatedShortUrl
from app.services.shorturl_service import create_short_url, get_short_url_content
from app.utils.rate_limiting.rate_limiting import (
    RateLimitPolicy,
    resolve_client_ip,
    validate_rate_limit,
)

router = APIRouter()

# 1 request per second per IP on the create endpoint, to curb spam. This endpoint
# is unauthenticated, so it is the only throttle there is.
_CREATE_POLICY = RateLimitPolicy(
    key_prefix="shorturl_create", limit=1, window_seconds=1
)


async def rate_limit_create_short_url(request: Request) -> None:
    await validate_rate_limit(resolve_client_ip(request), _CREATE_POLICY)


@router.post(
    path="",
    dependencies=[Depends(rate_limit_create_short_url)],
    summary="Create (or reuse) a short code for a pubkey + relay set",
)
async def create_short_url_endpoint(
    body: CreateShortUrlBody,
    db: AsyncDBSession = Depends(dependency=get_db),
) -> CreateShortUrlResponse:
    short_code, content = await create_short_url(db, body.pubkey, body.relays)
    return CreateShortUrlResponse(
        data=CreatedShortUrl(shortCode=short_code, content=content)
    )


@router.get(
    path="/{short_code}",
    summary="Resolve a short code to its stored pubkey + relays",
)
async def get_short_url_endpoint(
    short_code: str,
    db: AsyncDBSession = Depends(dependency=get_db),
) -> GetShortUrlResponse:
    content = await get_short_url_content(db, short_code)
    return GetShortUrlResponse(data=content)
