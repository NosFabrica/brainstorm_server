from fastapi import APIRouter, Depends, HTTPException, Request, status
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession as AsyncDBSession

from app.core.database import get_db
from app.repos.brainstorm_nsec import (
    get_graperank_preset_by_pubkey_on_db,
    set_graperank_preset_by_pubkey_on_db,
)
from app.schemas.request_response_schemas import SuccessfulResponseDataSchema
from app.services.graperank_presets import (
    ASSIGNABLE,
    GrapeRankPresetTemplate,
    normalize_preset,
)
from app.utils.auth.auth_models import JWTData


class PresetPayload(BaseModel):
    preset: str


class PresetData(BaseModel):
    preset: str


class PresetResponse(SuccessfulResponseDataSchema):
    data: PresetData


router = APIRouter()


@router.get(
    path="/preset",
    tags=[],
    dependencies=[],
    summary="Get the current user's GrapeRank preset",
)
async def get_graperank_preset_endpoint(
    request: Request,
    db: AsyncDBSession = Depends(dependency=get_db),
) -> PresetResponse:
    jwt_data: JWTData = request.state.jwt_data
    stored = await get_graperank_preset_by_pubkey_on_db(db, jwt_data.nostr_pubkey)
    preset = normalize_preset(stored)
    return PresetResponse(data=PresetData(preset=preset.value))


@router.put(
    path="/preset",
    tags=[],
    dependencies=[],
    summary="Set the current user's GrapeRank preset",
)
async def set_graperank_preset_endpoint(
    body: PresetPayload,
    request: Request,
    db: AsyncDBSession = Depends(dependency=get_db),
) -> PresetResponse:
    try:
        preset = GrapeRankPresetTemplate(body.preset.upper())
    except ValueError:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Unknown preset '{body.preset}'",
        )

    if preset not in ASSIGNABLE:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Preset '{preset.value}' is not assignable via this endpoint",
        )

    jwt_data: JWTData = request.state.jwt_data
    await set_graperank_preset_by_pubkey_on_db(db, jwt_data.nostr_pubkey, preset.value)
    return PresetResponse(data=PresetData(preset=preset.value))


@router.put(
    path="/preset/custom",
    tags=[],
    dependencies=[],
    summary="Set a custom GrapeRank preset (not implemented)",
)
async def set_custom_graperank_preset_endpoint(
    request: Request,
) -> None:
    raise HTTPException(
        status_code=status.HTTP_501_NOT_IMPLEMENTED,
        detail="Custom preset not implemented yet",
    )
