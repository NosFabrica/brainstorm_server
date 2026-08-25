"""Inbound Flash subscription webhooks.

Unauthenticated by our own JWT — the HMAC signature is the authentication.
Order is verify → record → commit → acknowledge, so an event survives the
process dying straight after the 200.
"""

import time

from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy.ext.asyncio import AsyncSession as AsyncDBSession

from app.core.config import settings
from app.core.database import get_db
from app.core.loggr import loggr
from app.services.flash_webhook_service import (
    SIGNATURE_HEADER,
    FlashSignatureError,
    handle_delivery,
    verify_delivery,
)

router = APIRouter()
logger = loggr.get_logger(__name__)


@router.post(
    path="/flash",
    summary="Receive a Flash subscription webhook",
    include_in_schema=False,
)
async def receive_flash_webhook(
    request: Request,
    db: AsyncDBSession = Depends(dependency=get_db),
):
    # Exact bytes — re-serializing would break the signature. Starlette caches
    # the body, so awaiting it here is safe.
    raw_body = await request.body()

    try:
        parts = verify_delivery(
            signature_header=request.headers.get(SIGNATURE_HEADER),
            raw_body=raw_body,
            secret=settings.flash_webhook_secret,
            now=int(time.time()),
            tolerance_seconds=settings.flash_webhook_tolerance_seconds,
        )
    except FlashSignatureError as rejected:
        # Never echo the expected signature or the secret — a rejected delivery
        # is the one case where someone hostile is reading the reply.
        logger.warning("Flash webhook rejected: %s", rejected.reason)
        raise HTTPException(status_code=rejected.status_code, detail=rejected.reason)

    recorded = await handle_delivery(
        db, raw_body=raw_body, delivery_timestamp=parts.timestamp
    )
    # Body is not wrapped in SuccessfulResponseDataSchema: like the .well-known
    # and NIP-11 routes, the consumer is an external system, not our client.
    # Flash reads only the status code.
    return {"ok": True, "duplicate": recorded.duplicate}
