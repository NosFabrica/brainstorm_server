from app.schemas.request_response_schemas import (
    GrapeRankPresetItem,
    GrapeRankPresetsData,
)
from app.services.graperank_presets import (
    PRESET_DEFINITIONS,
    GrapeRankPresetTemplate,
)


def list_graperank_presets() -> GrapeRankPresetsData:
    presets = [
        GrapeRankPresetItem(id=template, params=PRESET_DEFINITIONS[template])
        for template in (
            GrapeRankPresetTemplate.DEFAULT,
            GrapeRankPresetTemplate.PERMISSIVE,
            GrapeRankPresetTemplate.RESTRICTIVE,
        )
    ]
    return GrapeRankPresetsData(presets=presets, custom=None)
