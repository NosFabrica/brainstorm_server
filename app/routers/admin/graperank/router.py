from fastapi import APIRouter, Depends, HTTPException, Request, status
from sqlalchemy.ext.asyncio import AsyncSession as AsyncDBSession

from app.core.database import get_db
from app.repos.graperank_preset_repo import (
    get_preset_history_on_db,
    get_preset_on_db,
    row_to_camel_dict,
)
from app.schemas.request_response_schemas import (
    AdminPreset,
    AdminPresetHistoryData,
    AdminPresetHistoryEntry,
    AdminPresetHistoryResponse,
    AdminPresetItem,
    AdminPresetItemResponse,
    AdminPresetResponse,
)
from app.services.graperank_presets import (
    BuiltinPresetTemplate,
    GrapeRankPresetParams,
    update_preset_params,
)
from app.utils.auth.auth_models import JWTData


router = APIRouter()


@router.put(
    path="/preset/{preset_id}",
    summary="Admin: update a GrapeRank preset's parameters",
)
async def update_graperank_preset_endpoint(
    preset_id: BuiltinPresetTemplate,
    body: GrapeRankPresetParams,
    request: Request,
    db: AsyncDBSession = Depends(dependency=get_db),
) -> AdminPresetResponse:
    jwt_data: JWTData = request.state.jwt_data
    await update_preset_params(db, preset_id, body, jwt_data.nostr_pubkey)
    return AdminPresetResponse(data=AdminPreset(preset=preset_id))


@router.get(
    path="/preset/{preset_id}/history",
    summary="Admin: list change history for a GrapeRank preset (newest first)",
)
async def get_graperank_preset_history_endpoint(
    preset_id: BuiltinPresetTemplate,
    db: AsyncDBSession = Depends(dependency=get_db),
) -> AdminPresetHistoryResponse:
    rows = await get_preset_history_on_db(db, preset_id.value)
    entries = [
        AdminPresetHistoryEntry(
            id=row.id,
            presetId=BuiltinPresetTemplate(row.preset_id),
            params=GrapeRankPresetParams(**row_to_camel_dict(row)),
            changeType=row.change_type,
            changedBy=row.changed_by,
            changedAt=row.changed_at.isoformat(),
        )
        for row in rows
    ]
    return AdminPresetHistoryResponse(
        data=AdminPresetHistoryData(entries=entries),
    )


@router.get(
    path="/preset/{preset_id}",
    summary="Admin: get a single GrapeRank preset's current parameters",
)
async def get_graperank_preset_endpoint(
    preset_id: BuiltinPresetTemplate,
    db: AsyncDBSession = Depends(dependency=get_db),
) -> AdminPresetItemResponse:
    row = await get_preset_on_db(db, preset_id.value)
    if row is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Preset '{preset_id.value}' not found",
        )
    return AdminPresetItemResponse(
        data=AdminPresetItem(
            id=preset_id,
            params=GrapeRankPresetParams(**row_to_camel_dict(row)),
        ),
    )
