from fastapi import APIRouter, Depends, HTTPException, Request, status
from sqlalchemy.ext.asyncio import AsyncSession as AsyncDBSession

from app.core.database import get_db
from app.repos.brainstorm_nsec import (
    get_graperank_preset_by_pubkey_on_db,
    set_graperank_preset_by_pubkey_on_db,
)
from app.schemas.request_body_schemas import SetGrapeRankPresetBody
from app.schemas.request_response_schemas import (
    GrapeRankPreset,
    GrapeRankPresetResponse,
    GrapeRankPresetsResponse,
)
from app.services.graperank_preset_service import list_graperank_presets
from app.services.graperank_presets import (
    GrapeRankPresetTemplate,
    normalize_preset,
)
from app.utils.auth.auth_models import JWTData


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
) -> GrapeRankPresetResponse:
    jwt_data: JWTData = request.state.jwt_data
    stored = await get_graperank_preset_by_pubkey_on_db(db, jwt_data.nostr_pubkey)
    preset = normalize_preset(stored)
    return GrapeRankPresetResponse(data=GrapeRankPreset(preset=preset))


@router.put(
    path="/preset",
    tags=[],
    dependencies=[],
    summary="Set the current user's GrapeRank preset",
)
async def set_graperank_preset_endpoint(
    body: SetGrapeRankPresetBody,
    request: Request,
    db: AsyncDBSession = Depends(dependency=get_db),
) -> GrapeRankPresetResponse:
    jwt_data: JWTData = request.state.jwt_data
    await set_graperank_preset_by_pubkey_on_db(db, jwt_data.nostr_pubkey, body.preset.value)
    return GrapeRankPresetResponse(
        data=GrapeRankPreset(preset=GrapeRankPresetTemplate(body.preset.value)),
    )


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


@router.get(
    path="/presets",
    tags=[],
    dependencies=[],
    summary="List all GrapeRank presets with their parameter values",
)
async def get_graperank_presets_endpoint(
    request: Request,
    db: AsyncDBSession = Depends(dependency=get_db),
) -> GrapeRankPresetsResponse:
    # JWT required by router-level dependency. Per-user custom preset
    # lookup will use request.state.jwt_data in a follow-up.
    return GrapeRankPresetsResponse(data=await list_graperank_presets(db))
