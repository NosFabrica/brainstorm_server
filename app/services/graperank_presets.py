from enum import Enum

from pydantic import BaseModel, ConfigDict
from sqlalchemy.ext.asyncio import AsyncSession as AsyncDBSession

from app.repos.graperank_preset_repo import (
    get_preset_on_db,
    row_to_camel_dict,
    update_preset_on_db,
)


class GrapeRankPresetTemplate(str, Enum):
    DEFAULT = "DEFAULT"
    PERMISSIVE = "PERMISSIVE"
    RESTRICTIVE = "RESTRICTIVE"
    CUSTOM = "CUSTOM"


# Built-in presets — each has a row in graperank_preset, seeded by migration,
# admin-editable. Excludes CUSTOM (per-user, separate endpoint).
class BuiltinPresetTemplate(str, Enum):
    DEFAULT = "DEFAULT"
    PERMISSIVE = "PERMISSIVE"
    RESTRICTIVE = "RESTRICTIVE"


class GrapeRankPresetParams(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    rigor: float
    attenuationFactor: float
    followRating: float
    followConfidence: float
    muteRating: float
    muteConfidence: float
    reportRating: float
    reportConfidence: float
    followConfidenceOfObserver: float
    verifiedFollowersInfluenceCutoff: float
    verifiedReportersInfluenceCutoff: float
    verifiedMutersInfluenceCutoff: float


async def resolve_preset_params(
    db: AsyncDBSession,
    preset: GrapeRankPresetTemplate,
) -> GrapeRankPresetParams:
    if preset == GrapeRankPresetTemplate.CUSTOM:
        raise NotImplementedError("Custom preset params lookup not implemented yet")

    row = await get_preset_on_db(db, preset.value)
    if row is None:
        raise RuntimeError(
            f"graperank_preset row missing for {preset.value} — "
            "alembic seed migration likely not applied"
        )
    return GrapeRankPresetParams(**row_to_camel_dict(row))


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
