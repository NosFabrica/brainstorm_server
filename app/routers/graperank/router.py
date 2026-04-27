from fastapi import APIRouter, Depends, HTTPException, Request, status
from sqlalchemy.ext.asyncio import AsyncSession as AsyncDBSession

from app.core.database import get_db
from app.routers.admin.router import verify_admin_access
from app.repos.brainstorm_nsec import (
    get_graperank_custom_params_by_pubkey_on_db,
    get_graperank_preset_by_pubkey_on_db,
    set_graperank_custom_params_by_pubkey_on_db,
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
    GrapeRankPresetParams,
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

    if body.preset == GrapeRankPresetTemplate.CUSTOM:
        stored = await get_graperank_custom_params_by_pubkey_on_db(
            db, jwt_data.nostr_pubkey
        )
        if stored is None:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="No custom params stored — call PUT /user/graperank/preset/custom with values first",
            )

    await set_graperank_preset_by_pubkey_on_db(db, jwt_data.nostr_pubkey, body.preset.value)
    return GrapeRankPresetResponse(data=GrapeRankPreset(preset=body.preset))


@router.put(
    path="/preset/custom",
    tags=[],
    # TODO: gate custom preset feature — admin-only for now
    dependencies=[Depends(verify_admin_access)],
    summary="Set the current user's CUSTOM GrapeRank preset values",
    status_code=status.HTTP_204_NO_CONTENT,
)
async def set_custom_graperank_preset_endpoint(
    body: GrapeRankPresetParams,
    request: Request,
    db: AsyncDBSession = Depends(dependency=get_db),
) -> None:
    jwt_data: JWTData = request.state.jwt_data
    await set_graperank_custom_params_by_pubkey_on_db(
        db, jwt_data.nostr_pubkey, body.model_dump()
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
    jwt_data: JWTData = request.state.jwt_data
    return GrapeRankPresetsResponse(
        data=await list_graperank_presets(db, jwt_data.nostr_pubkey)
    )
