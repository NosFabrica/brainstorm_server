"""The vouch params' place in the preset contract: full shape on the wire,
factory defaults filling pre-vouch CUSTOM snapshots."""

import pytest
from pydantic import ValidationError

from app.schemas.graperank_schemas import GrapeRankPresetParams

FULL = {
    "rigor": 0.5,
    "attenuationFactor": 0.85,
    "followRating": 1.0,
    "followConfidence": 0.03,
    "muteRating": -0.1,
    "muteConfidence": 0.5,
    "reportRating": -0.1,
    "reportConfidence": 0.5,
    "followConfidenceOfObserver": 0.5,
    "verifiedFollowersInfluenceCutoff": 0.02,
    "verifiedReportersInfluenceCutoff": 0.1,
    "verifiedMutersInfluenceCutoff": 0.01,
    "vouchRating": 1.0,
    "vouchConfidence": 0.4,
}


def test_full_shape_round_trips_unchanged():
    assert GrapeRankPresetParams(**FULL).model_dump() == FULL


def test_a_pre_vouch_custom_snapshot_gets_the_factory_defaults():
    legacy = {k: v for k, v in FULL.items() if not k.startswith("vouch")}

    params = GrapeRankPresetParams(**legacy)

    assert params.vouchRating == 1.0
    assert params.vouchConfidence == 0.4
    # model_dump writes the full shape, so the Java worker's required-fields
    # contract still holds for a legacy snapshot.
    assert params.model_dump() == FULL


def test_an_unknown_field_still_fails_fast():
    with pytest.raises(ValidationError):
        GrapeRankPresetParams(**{**FULL, "voucheRating": 0.9})


def test_out_of_range_vouch_values_are_rejected():
    with pytest.raises(ValidationError):
        GrapeRankPresetParams(**{**FULL, "vouchConfidence": 1.5})
    with pytest.raises(ValidationError):
        GrapeRankPresetParams(**{**FULL, "vouchRating": -1.1})
