"""Preset → verified-cutoff resolution.

The three verified counts (`followers`, `muters`, `reporters`) are derived from
the observer's *server-saved* GrapeRank preset, not from a client-supplied flat
threshold. This covers the resolution itself, driven by the values the seed
migration actually writes; the endpoint behaviour it feeds lives in
``tests/integration/test_preset_stats_integration.py``.

Only the two DB reads are stubbed — the real `resolve_preset_params` and the
snake→camel column bridge run, so a column rename or a changed seed value fails
here rather than silently shifting every verified count in production.
"""

import asyncio
import importlib.util
from pathlib import Path

import pytest

from app.db_models import GrapeRankPreset
from app.schemas.graperank_schemas import GrapeRankPresetParams
from app.services.verified_cutoffs import (
    FALLBACK_VERIFIED_CUTOFFS,
    VerifiedCutoffs,
    build_cutoffs_from_params,
    resolve_verified_cutoffs,
)


def _load_seeded_presets() -> dict[str, dict[str, float]]:
    """The factory preset values, read from the seed migration itself."""
    path = (
        Path(__file__).resolve().parent.parent
        / "alembic/versions/4fcfe9570a50_add_graperank_preset_tables.py"
    )
    spec = importlib.util.spec_from_file_location("_preset_seed_migration", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module._SEED


SEED = _load_seeded_presets()


def _params(**overrides) -> GrapeRankPresetParams:
    base = dict(
        rigor=0.25,
        attenuationFactor=0.85,
        followRating=1.0,
        followConfidence=0.03,
        muteRating=-1.0,
        muteConfidence=0.5,
        reportRating=-1.0,
        reportConfidence=0.5,
        followConfidenceOfObserver=0.5,
        verifiedFollowersInfluenceCutoff=0.02,
        verifiedReportersInfluenceCutoff=0.1,
        verifiedMutersInfluenceCutoff=0.01,
    )
    base.update(overrides)
    return GrapeRankPresetParams(**base)


# ---------------------------------------------------------------------------
# Pure mapping
# ---------------------------------------------------------------------------
def test_each_cutoff_comes_from_its_own_preset_param():
    cutoffs = build_cutoffs_from_params(
        _params(
            verifiedFollowersInfluenceCutoff=0.11,
            verifiedMutersInfluenceCutoff=0.22,
            verifiedReportersInfluenceCutoff=0.33,
        )
    )
    assert cutoffs == VerifiedCutoffs(follower=0.11, muter=0.22, reporter=0.33)


def test_verified_line_is_the_follower_cutoff():
    assert build_cutoffs_from_params(_params()).verified_line == 0.02


def test_outbound_sections_use_the_follower_cutoff():
    cutoffs = VerifiedCutoffs(follower=0.11, muter=0.22, reporter=0.33)
    assert cutoffs.for_kind("following") == 0.11
    assert cutoffs.for_kind("muting") == 0.11
    assert cutoffs.for_kind("reporting") == 0.11


def test_inbound_sections_use_their_own_relationship_cutoff():
    cutoffs = VerifiedCutoffs(follower=0.11, muter=0.22, reporter=0.33)
    assert cutoffs.for_kind("followed_by") == 0.11
    assert cutoffs.for_kind("muted_by") == 0.22
    assert cutoffs.for_kind("reported_by") == 0.33


def test_unknown_kind_raises_rather_than_defaulting():
    # A silent fallback to the follower cutoff would be a wrong count.
    with pytest.raises(KeyError):
        VerifiedCutoffs(follower=0.1, muter=0.2, reporter=0.3).for_kind("flagged")


def test_kind_map_covers_every_stats_section():
    from app.repos.user_repo import _STATS_KINDS

    cutoffs = VerifiedCutoffs(follower=0.1, muter=0.2, reporter=0.3)
    assert set(cutoffs.as_kind_map()) == {name for name, _, _ in _STATS_KINDS}


# ---------------------------------------------------------------------------
# The seeded presets, end to end through the real resolution path
# ---------------------------------------------------------------------------
@pytest.fixture
def saved_presets(monkeypatch):
    """Stub only the two DB reads; returns a mutable {pubkey: preset_name} store."""
    store: dict[str, str | None] = {}

    async def _get_saved(_db, pubkey):
        return store.get(pubkey)

    async def _get_preset_row(_db, preset_id):
        return GrapeRankPreset(id=preset_id, **SEED[preset_id])

    monkeypatch.setattr(
        "app.services.verified_cutoffs.get_graperank_preset_by_pubkey_on_db",
        _get_saved,
    )
    monkeypatch.setattr(
        "app.services.graperank_preset_service.get_preset_on_db", _get_preset_row
    )
    return store


def _resolve(observer: str | None) -> VerifiedCutoffs:
    return asyncio.run(resolve_verified_cutoffs(None, observer))


def test_resolves_the_observers_saved_preset(saved_presets):
    saved_presets["obs"] = "RESTRICTIVE"
    assert _resolve("obs") == VerifiedCutoffs(follower=0.5, muter=0.5, reporter=0.5)


def test_default_preset_keeps_the_historical_verified_line(saved_presets):
    saved_presets["obs"] = "DEFAULT"
    assert _resolve("obs") == VerifiedCutoffs(follower=0.02, muter=0.01, reporter=0.1)


def test_no_validity_floor_is_applied(saved_presets):
    # 0.02 is the publish/validity floor — verified is orthogonal to it, so
    # PERMISSIVE's cutoffs below it must survive unclamped.
    saved_presets["obs"] = "PERMISSIVE"
    assert _resolve("obs") == VerifiedCutoffs(
        follower=0.002, muter=0.002, reporter=0.002
    )


def test_switching_preset_moves_every_cutoff(saved_presets):
    saved_presets["obs"] = "DEFAULT"
    before = _resolve("obs")
    saved_presets["obs"] = "RESTRICTIVE"
    after = _resolve("obs")

    assert after.follower > before.follower
    assert after.muter > before.muter
    assert after.reporter > before.reporter


def test_observer_with_no_saved_preset_gets_default(saved_presets):
    assert _resolve("never-seen") == _resolve_default(saved_presets)


def test_anonymous_observer_still_resolves_a_preset(saved_presets):
    # `None` (no observer at all) must not blow up either.
    assert _resolve(None) == _resolve_default(saved_presets)


def _resolve_default(saved_presets) -> VerifiedCutoffs:
    saved_presets["explicit-default"] = "DEFAULT"
    return _resolve("explicit-default")


def test_unreadable_preset_falls_back_instead_of_failing_the_read(monkeypatch):
    async def _boom(*_args, **_kwargs):
        raise RuntimeError("graperank_preset row missing")

    monkeypatch.setattr(
        "app.services.verified_cutoffs.get_graperank_preset_by_pubkey_on_db", _boom
    )
    assert _resolve("obs") == FALLBACK_VERIFIED_CUTOFFS


def test_fallback_matches_the_seeded_default(monkeypatch):
    # The fallback is only reachable when the preset table can't be read, so it
    # must not quietly disagree with what DEFAULT would have said.
    assert FALLBACK_VERIFIED_CUTOFFS == build_cutoffs_from_params(
        GrapeRankPresetParams(
            **{
                camel: SEED["DEFAULT"][snake]
                for camel, snake in _column_map().items()
            }
        )
    )


def _column_map() -> dict[str, str]:
    from app.repos.graperank_preset_repo import COLUMN_MAP

    return COLUMN_MAP
