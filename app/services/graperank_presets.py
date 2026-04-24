from enum import Enum
from types import MappingProxyType
from typing import Mapping

from pydantic import BaseModel, ConfigDict


class GrapeRankPresetTemplate(str, Enum):
    DEFAULT = "DEFAULT"
    PERMISSIVE = "PERMISSIVE"
    RESTRICTIVE = "RESTRICTIVE"
    CUSTOM = "CUSTOM"


ASSIGNABLE: set[GrapeRankPresetTemplate] = {
    GrapeRankPresetTemplate.DEFAULT,
    GrapeRankPresetTemplate.PERMISSIVE,
    GrapeRankPresetTemplate.RESTRICTIVE,
}


class GrapeRankPresetParams(BaseModel):
    model_config = ConfigDict(frozen=True)

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


_PRESET_DEFINITIONS: dict[GrapeRankPresetTemplate, GrapeRankPresetParams] = {
    GrapeRankPresetTemplate.DEFAULT: GrapeRankPresetParams(
        rigor=0.5,
        attenuationFactor=0.85,
        followRating=1.0,
        followConfidence=0.03,
        muteRating=-0.1,
        muteConfidence=0.5,
        reportRating=-0.1,
        reportConfidence=0.5,
        followConfidenceOfObserver=0.5,
        verifiedFollowersInfluenceCutoff=0.02,
        verifiedReportersInfluenceCutoff=0.1,
        verifiedMutersInfluenceCutoff=0.01,
    ),
    GrapeRankPresetTemplate.PERMISSIVE: GrapeRankPresetParams(
        rigor=0.3,
        attenuationFactor=0.95,
        followRating=1.0,
        followConfidence=0.1,
        muteRating=0.0,
        muteConfidence=0.1,
        reportRating=0.0,
        reportConfidence=0.1,
        followConfidenceOfObserver=0.1,
        verifiedFollowersInfluenceCutoff=0.002,
        verifiedReportersInfluenceCutoff=0.002,
        verifiedMutersInfluenceCutoff=0.002,
    ),
    GrapeRankPresetTemplate.RESTRICTIVE: GrapeRankPresetParams(
        rigor=0.65,
        attenuationFactor=0.5,
        followRating=1.0,
        followConfidence=0.03,
        muteRating=-0.9,
        muteConfidence=0.9,
        reportRating=-0.9,
        reportConfidence=0.9,
        followConfidenceOfObserver=0.5,
        verifiedFollowersInfluenceCutoff=0.5,
        verifiedReportersInfluenceCutoff=0.5,
        verifiedMutersInfluenceCutoff=0.5,
    ),
}

PRESET_DEFINITIONS: Mapping[GrapeRankPresetTemplate, GrapeRankPresetParams] = (
    MappingProxyType(_PRESET_DEFINITIONS)
)


def normalize_preset(raw: str | None) -> GrapeRankPresetTemplate:
    if not raw:
        return GrapeRankPresetTemplate.DEFAULT
    try:
        return GrapeRankPresetTemplate(raw.upper())
    except ValueError:
        return GrapeRankPresetTemplate.DEFAULT


def resolve_preset_params(preset: GrapeRankPresetTemplate) -> GrapeRankPresetParams:
    if preset == GrapeRankPresetTemplate.CUSTOM:
        raise NotImplementedError("Custom preset params lookup not implemented yet")
    return PRESET_DEFINITIONS[preset]
