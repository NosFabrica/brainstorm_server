from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession as AsyncDBSession

from app.core.database import execute_db_statement
from app.db_models import GrapeRankPreset, GrapeRankPresetHistory


# camelCase ↔ snake_case mapping. The pydantic GrapeRankPresetParams uses
# camelCase to match the Java record; DB columns are snake_case per project convention.
# Single source of truth for the mapping lives here so the service layer can work in camelCase end-to-end.
COLUMN_MAP: dict[str, str] = {
    "rigor": "rigor",
    "attenuationFactor": "attenuation_factor",
    "followRating": "follow_rating",
    "followConfidence": "follow_confidence",
    "muteRating": "mute_rating",
    "muteConfidence": "mute_confidence",
    "reportRating": "report_rating",
    "reportConfidence": "report_confidence",
    "followConfidenceOfObserver": "follow_confidence_of_observer",
    "verifiedFollowersInfluenceCutoff": "verified_followers_influence_cutoff",
    "verifiedReportersInfluenceCutoff": "verified_reporters_influence_cutoff",
    "verifiedMutersInfluenceCutoff": "verified_muters_influence_cutoff",
}


def row_to_camel_dict(row: GrapeRankPreset) -> dict[str, float]:
    return {camel: getattr(row, snake) for camel, snake in COLUMN_MAP.items()}


def camel_dict_to_columns(params: dict[str, float]) -> dict[str, float]:
    return {COLUMN_MAP[camel]: value for camel, value in params.items()}


async def get_all_presets_on_db(db: AsyncDBSession) -> list[GrapeRankPreset]:
    stmt = select(GrapeRankPreset)
    result = await execute_db_statement(db, stmt, __name__)
    return list(result.scalars().all())


async def get_preset_on_db(
    db: AsyncDBSession, preset_id: str
) -> GrapeRankPreset | None:
    stmt = select(GrapeRankPreset).where(GrapeRankPreset.id == preset_id)
    result = await execute_db_statement(db, stmt, __name__)
    return result.scalar_one_or_none()


async def update_preset_on_db(
    db: AsyncDBSession,
    preset_id: str,
    params_camel: dict[str, float],
    changed_by: str | None,
) -> GrapeRankPreset:
    row = await get_preset_on_db(db, preset_id)
    if row is None:
        raise ValueError(f"Preset {preset_id} not found")

    columns = camel_dict_to_columns(params_camel)
    for column_name, value in columns.items():
        setattr(row, column_name, value)

    history = GrapeRankPresetHistory(
        preset_id=preset_id,
        change_type="UPDATE",
        changed_by=changed_by,
        **columns,
    )
    db.add(history)

    await db.flush()
    await db.refresh(row)
    return row


async def get_preset_history_on_db(
    db: AsyncDBSession, preset_id: str, limit: int = 100
) -> list[GrapeRankPresetHistory]:
    stmt = (
        select(GrapeRankPresetHistory)
        .where(GrapeRankPresetHistory.preset_id == preset_id)
        .order_by(GrapeRankPresetHistory.changed_at.desc())
        .limit(limit)
    )
    result = await execute_db_statement(db, stmt, __name__)
    return list(result.scalars().all())
