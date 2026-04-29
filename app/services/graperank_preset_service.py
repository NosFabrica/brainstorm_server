from sqlalchemy.ext.asyncio import AsyncSession as AsyncDBSession

from app.core.loggr import loggr
from app.repos.brainstorm_nsec import get_graperank_custom_params_by_pubkey_on_db
from app.repos.graperank_preset_repo import (
    get_all_presets_on_db,
    get_preset_on_db,
    row_to_camel_dict,
    update_preset_on_db,
)
from app.schemas.graperank_schemas import (
    BuiltinPresetTemplate,
    GrapeRankPresetParams,
    GrapeRankPresetTemplate,
)
from app.schemas.request_response_schemas import (
    GrapeRankPresetItem,
    GrapeRankPresetsData,
)

logger = loggr.get_logger(__name__)


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


async def resolve_preset_params(
    db: AsyncDBSession,
    preset: GrapeRankPresetTemplate,
    pubkey: str | None = None,
) -> tuple[GrapeRankPresetTemplate, GrapeRankPresetParams]:
    """Returns (effective_preset, params).

    `effective_preset` may differ from `preset` when CUSTOM is requested but no
    custom params are stored — caller should record the effective value for audit.
    """
    if preset == GrapeRankPresetTemplate.CUSTOM:
        if pubkey is None:
            raise ValueError("pubkey is required to resolve CUSTOM preset")
        custom_params = await get_graperank_custom_params_by_pubkey_on_db(db, pubkey)
        if custom_params is not None:
            return preset, GrapeRankPresetParams(**custom_params)
        # PUT /preset rejects CUSTOM without stored params, so this is stale
        # state — fall back to DEFAULT for this request, no DB write.
        logger.warning(
            "User %s has preset=CUSTOM but no custom params stored; "
            "falling back to DEFAULT for this request",
            pubkey,
        )
        preset = GrapeRankPresetTemplate.DEFAULT

    row = await get_preset_on_db(db, preset.value)
    if row is None:
        raise RuntimeError(
            f"graperank_preset row missing for {preset.value} — "
            "alembic seed migration likely not applied"
        )
    return preset, GrapeRankPresetParams(**row_to_camel_dict(row))


async def update_preset_params(
    db: AsyncDBSession,
    preset: BuiltinPresetTemplate,
    params: GrapeRankPresetParams,
    changed_by: str | None,
) -> GrapeRankPresetParams:
    row = await update_preset_on_db(
        db, preset.value, params.model_dump(), changed_by
    )
    return GrapeRankPresetParams(**row_to_camel_dict(row))


def normalize_preset(raw: str | None) -> GrapeRankPresetTemplate:
    if not raw:
        return GrapeRankPresetTemplate.DEFAULT
    try:
        return GrapeRankPresetTemplate(raw.upper())
    except ValueError:
        return GrapeRankPresetTemplate.DEFAULT
