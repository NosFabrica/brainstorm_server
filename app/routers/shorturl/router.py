from fastapi import APIRouter, Depends, Request

from app.schemas.request_body_schemas import CreateShortUrlBody
from app.schemas.request_response_schemas import (
    CreateShortUrlResponse,
    GetShortUrlResponse,
)
from app.schemas.schemas import CreatedShortUrl
from app.services.shorturl_service import (
    create_short_url,
    get_short_url_content,
)
from app.utils.rate_limiting.rate_limiting import validate_rate_limit

router = APIRouter()

# 1 request per second per IP on the create endpoint, to curb spam.
_CREATE_RATE_LIMIT = 1
_CREATE_RATE_WINDOW_SECONDS = 1
_CREATE_RATE_KEY_PREFIX = "shorturl_create"


def _client_ip(request: Request) -> str:
    """Best-effort client IP, honoring X-Forwarded-For behind a proxy."""
    forwarded = request.headers.get("x-forwarded-for")
    if forwarded:
        return forwarded.split(",")[0].strip()
    return request.client.host if request.client else "unknown"


async def rate_limit_create_short_url(request: Request) -> None:
    await validate_rate_limit(
        _client_ip(request),
        key_prefix=_CREATE_RATE_KEY_PREFIX,
        limit=_CREATE_RATE_LIMIT,
        window_seconds=_CREATE_RATE_WINDOW_SECONDS,
    )


@router.post(
    path="",
    dependencies=[Depends(rate_limit_create_short_url)],
    summary="Create (or reuse) a short code for a pubkey + relay set",
)
async def create_short_url_endpoint(
    body: CreateShortUrlBody,
) -> CreateShortUrlResponse:
    short_code, content = await create_short_url(body.pubkey, body.relays)
    return CreateShortUrlResponse(
        data=CreatedShortUrl(shortCode=short_code, content=content)
    )


@router.get(
    path="/{short_code}",
    summary="Resolve a short code to its stored pubkey + relays",
)
async def get_short_url_endpoint(short_code: str) -> GetShortUrlResponse:
    content = await get_short_url_content(short_code)
    return GetShortUrlResponse(data=content)
