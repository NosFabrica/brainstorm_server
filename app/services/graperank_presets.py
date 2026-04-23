from enum import Enum


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


def normalize_preset(raw: str | None) -> GrapeRankPresetTemplate:
    if not raw:
        return GrapeRankPresetTemplate.DEFAULT
    try:
        return GrapeRankPresetTemplate(raw.upper())
    except ValueError:
        return GrapeRankPresetTemplate.DEFAULT


def build_params_payload(preset: GrapeRankPresetTemplate) -> dict:
    return {"template": preset.value}
