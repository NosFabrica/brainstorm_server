import asyncio

from fastapi import APIRouter, HTTPException, status

from app.core.loggr import loggr
from app.services.nsec_encryption_service import (
    RotationFailed,
    is_rotation_running,
    rotate_key,
    verify_keys,
)

logger = loggr.get_logger(__name__)

router = APIRouter()


async def _run_rotation_background() -> None:
    try:
        await rotate_key()
    except Exception:
        logger.exception("nsec key rotation task failed")


@router.post(
    path="/rotate",
    status_code=status.HTTP_202_ACCEPTED,
    summary="Rotate the nsec-at-rest encryption key",
    description=(
        "Generates a new Fernet key and re-encrypts every `brainstorm_nsec` row. "
        "Runs as a background task; returns 202 immediately, 409 if already running. "
        "Copy the updated key file to the secrets vault after success."
    ),
)
async def rotate_nsec_key_endpoint() -> dict:
    if is_rotation_running():
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="nsec key rotation already in progress",
        )
    asyncio.create_task(_run_rotation_background())
    return {"status": "started"}


@router.post(
    path="/verify",
    summary="Verify every encrypted_nsec row decrypts under the current primary key.",
)
async def verify_nsec_keys_endpoint() -> dict:
    try:
        result = await verify_keys()
    except RotationFailed as e:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(e))
    return {"ok": result.ok, "fail": result.fail}
