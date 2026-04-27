from sqlalchemy.ext.asyncio import AsyncSession as AsyncDBSession

from app.repos.brainstorm_nsec import get_graperank_custom_params_by_pubkey_on_db
from app.repos.graperank_preset_repo import get_all_presets_on_db, row_to_camel_dict
from app.schemas.request_response_schemas import (
    GrapeRankPresetItem,
    GrapeRankPresetsData,
)
from app.services.graperank_presets import (
    BuiltinPresetTemplate,
    GrapeRankPresetParams,
    GrapeRankPresetTemplate,
)


async def list_graperank_presets(
    db: AsyncDBSession, pubkey: str
) -> GrapeRankPresetsData:
    rows = await get_all_presets_on_db(db)
    by_id = {row.id: row for row in rows}

    presets: list[GrapeRankPresetItem] = []
    for template in BuiltinPresetTemplate:
        row = by_id.get(template.value)
        if row is None:
            raise RuntimeError(
                f"graperank_preset row missing for {template.value} — "
                "alembic seed migration likely not applied"
            )
        presets.append(
            GrapeRankPresetItem(
                id=GrapeRankPresetTemplate(template.value),
                params=GrapeRankPresetParams(**row_to_camel_dict(row)),
            )
        )

    custom_raw = await get_graperank_custom_params_by_pubkey_on_db(db, pubkey)
    custom = (
        GrapeRankPresetItem(
            id=GrapeRankPresetTemplate.CUSTOM,
            params=GrapeRankPresetParams(**custom_raw),
        )
        if custom_raw
        else None
    )
    return GrapeRankPresetsData(presets=presets, custom=custom)
