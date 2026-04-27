from enum import Enum

from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.ext.asyncio import AsyncSession as AsyncDBSession

from app.core.loggr import loggr
from app.repos.brainstorm_nsec import get_graperank_custom_params_by_pubkey_on_db
from app.repos.graperank_preset_repo import (
    get_preset_on_db,
    row_to_camel_dict,
    update_preset_on_db,
)

logger = loggr.get_logger(__name__)


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

    rigor: float = Field(ge=0.0, le=1.0)
    attenuationFactor: float = Field(ge=0.0, le=1.0)
    followRating: float = Field(ge=-1.0, le=1.0)
    followConfidence: float = Field(ge=0.0, le=1.0)
    muteRating: float = Field(ge=-1.0, le=1.0)
    muteConfidence: float = Field(ge=0.0, le=1.0)
    reportRating: float = Field(ge=-1.0, le=1.0)
    reportConfidence: float = Field(ge=0.0, le=1.0)
    followConfidenceOfObserver: float = Field(ge=0.0, le=1.0)
    verifiedFollowersInfluenceCutoff: float = Field(ge=0.0, le=1.0)
    verifiedReportersInfluenceCutoff: float = Field(ge=0.0, le=1.0)
    verifiedMutersInfluenceCutoff: float = Field(ge=0.0, le=1.0)


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
