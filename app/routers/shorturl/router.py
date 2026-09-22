from fastapi import APIRouter, Depends, Path
from sqlalchemy.ext.asyncio import AsyncSession as AsyncDBSession

from app.core.database import get_db
from app.schemas.request_body_schemas import CreateShortUrlBody
from app.schemas.request_response_schemas import (
    CreateShortUrlResponse,
    GetShortUrlResponse,
)
from app.services.shorturl_service import create_short_url, get_short_url_content
from app.utils.rate_limiting.rate_limiting import RateLimitPolicy, rate_limit

router = APIRouter()

# Unauthenticated, so this is the only throttle.
rate_limit_create_short_url = rate_limit(
    RateLimitPolicy(key_prefix="shorturl_create", limit=1, window_seconds=1)
)


@router.post(
    path="",
    dependencies=[Depends(rate_limit_create_short_url)],
    summary="Create (or reuse) a short code for a pubkey + relay set",
)
async def create_short_url_endpoint(
    body: CreateShortUrlBody,
    db: AsyncDBSession = Depends(dependency=get_db),
) -> CreateShortUrlResponse:
    created = await create_short_url(db, body.pubkey, body.relays)
    return CreateShortUrlResponse(data=created)


@router.get(
    path="/{short_code}",
    summary="Resolve a short code to its stored pubkey + relays",
)
async def get_short_url_endpoint(
    short_code: str = Path(pattern=r"^[A-Za-z0-9]{6,32}$"),
    db: AsyncDBSession = Depends(dependency=get_db),
) -> GetShortUrlResponse:
    content = await get_short_url_content(db, short_code)
    return GetShortUrlResponse(data=content)
