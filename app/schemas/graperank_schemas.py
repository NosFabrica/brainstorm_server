from enum import Enum

from pydantic import BaseModel, ConfigDict, Field


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
