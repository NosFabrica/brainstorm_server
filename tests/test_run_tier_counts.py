"""`count_values` buckets a run's scorecards on that run's own preset line.

The per-hop confidence buckets stored on a BrainstormRequest are the run's
historical snapshot of the observer's graph. They used to bucket against the
flat 0.02 constant, which agreed with `/stats` only for a DEFAULT observer —
once /stats became preset-driven, a RESTRICTIVE observer's stored history and
their live page disagreed.

Issue: .scratch/preset-verified-counts/issues/04-frontend-render-backend-truth.md
"""

import asyncio
import contextlib
import json
from unittest.mock import AsyncMock, patch

import pytest

from app.core.tier_thresholds import DEFAULT_VERIFIED_THRESHOLD
from app.message_queue_tasks.message_queue_consumer import (
    bucket_scorecards_by_confidence_and_hops,
    process_message,
    verified_line_for_run,
)
from app.models.grapeRankResult import ScoreCard

_CONSUMER = "app.message_queue_tasks.message_queue_consumer"

OBSERVER = "a" * 64


def _params(follower_cutoff: float) -> dict:
    """A `graperank_params` snapshot, as `GrapeRankPresetParams.model_dump()`
    writes it onto the request row."""
    return {
        "rigor": 0.5,
        "attenuationFactor": 0.5,
        "followRating": 1.0,
        "followConfidence": 0.03,
        "muteRating": -1.0,
        "muteConfidence": 0.5,
        "reportRating": -1.0,
        "reportConfidence": 0.5,
        "followConfidenceOfObserver": 0.5,
        "verifiedFollowersInfluenceCutoff": follower_cutoff,
        "verifiedReportersInfluenceCutoff": 0.1,
        "verifiedMutersInfluenceCutoff": 0.01,
    }


def _card(observee: str, influence: float, hops: int = 1, reporters: int = 0) -> ScoreCard:
    return ScoreCard(
        observer=OBSERVER,
        observee=observee,
        influence=influence,
        hops=hops,
        trusted_reporters=reporters,
    )


class _Row:
    def __init__(self, graperank_params):
        self.graperank_params = graperank_params


class TestVerifiedLineForRun:
    def test_uses_the_runs_own_snapshotted_cutoff(self):
        # The run's params, not today's preset — count_values is history, so a
        # later preset change must not retroactively rewrite what it meant.
        assert verified_line_for_run(_Row(_params(0.5))) == 0.5

    def test_falls_back_to_the_baseline_when_the_row_has_no_params(self):
        # Rows predating the params snapshot, and the row-not-found case.
        assert verified_line_for_run(_Row(None)) == DEFAULT_VERIFIED_THRESHOLD
        assert verified_line_for_run(None) == DEFAULT_VERIFIED_THRESHOLD

    def test_falls_back_when_the_snapshot_is_unusable(self):
        # A truncated or renamed snapshot must not take the consumer down: the
        # run's result still has to be written.
        assert verified_line_for_run(_Row({"rigor": 0.5})) == DEFAULT_VERIFIED_THRESHOLD


class TestBucketing:
    def test_bands_apply_above_the_line(self):
        cards = {
            "1": _card("obs_high", 0.6),
            "2": _card("obs_medium_high", 0.3),
            "3": _card("obs_medium", 0.1),
            "4": _card("obs_medium_low", 0.03),
        }

        counts, _ = bucket_scorecards_by_confidence_and_hops(cards, verified_line=0.02)

        assert counts["high"] == {1: 1}
        assert counts["medium_high"] == {1: 1}
        assert counts["medium"] == {1: 1}
        assert counts["medium_low"] == {1: 1}

    def test_a_stricter_run_line_falls_subjects_through_to_unverified(self):
        # The whole point: at 0.02 these are banded; at 0.5 they are not, and
        # the one with 2 trusted reporters is flagged rather than low.
        cards = {
            "1": _card("obs_a", 0.3),
            "2": _card("obs_b", 0.1, reporters=2),
        }

        default = bucket_scorecards_by_confidence_and_hops(cards, verified_line=0.02)[0]
        strict = bucket_scorecards_by_confidence_and_hops(cards, verified_line=0.5)[0]

        assert default["medium_high"] == {1: 1}
        assert default["medium"] == {1: 1}
        assert strict["medium_high"] == {}
        assert strict["medium"] == {}
        assert strict["low"] == {1: 1}
        assert strict["low_and_reported_by_2_or_more_trusted_pubkeys"] == {1: 1}

    def test_a_subject_exactly_on_the_line_is_not_banded(self):
        # Strict `>`, matching GrapeRank and the read endpoints.
        cards = {"1": _card("obs_on_line", 0.5)}

        counts, _ = bucket_scorecards_by_confidence_and_hops(cards, verified_line=0.5)

        assert counts["low"] == {1: 1}
        assert counts["high"] == {}

    def test_counts_are_split_by_hops(self):
        cards = {
            "1": _card("obs_a", 0.6, hops=1),
            "2": _card("obs_b", 0.7, hops=1),
            "3": _card("obs_c", 0.8, hops=3),
        }

        counts, _ = bucket_scorecards_by_confidence_and_hops(cards, verified_line=0.02)

        assert counts["high"] == {1: 2, 3: 1}

    def test_whitelist_holds_the_above_cutoff_observees_at_rounded_influence(self):
        # Unchanged by the line: the whitelist is gated on the *validity* cutoff
        # (settings.cutoff_of_valid_graperank_scores), a different bar.
        cards = {"1": _card("obs_a", 0.6), "2": _card("obs_b", 0.0)}

        _, whitelist = bucket_scorecards_by_confidence_and_hops(cards, verified_line=0.5)

        assert whitelist["obs_a"] == 0.6
        assert "obs_b" not in whitelist

    def test_no_scorecards_yields_empty_buckets_not_a_crash(self):
        counts, whitelist = bucket_scorecards_by_confidence_and_hops(
            None, verified_line=0.02
        )

        assert whitelist == {}
        assert all(hops_counts == {} for hops_counts in counts.values())


def test_bucket_keys_are_the_canonical_tier_names():
    from app.core.tier_thresholds import TIER_NAMES

    counts, _ = bucket_scorecards_by_confidence_and_hops({}, verified_line=0.02)

    assert list(counts) == list(TIER_NAMES)


class _FakeSession:
    async def commit(self):
        return None


@contextlib.asynccontextmanager
async def _fake_db_session():
    yield _FakeSession()


def test_process_message_buckets_against_the_runs_own_line():
    """The wiring, not just the helpers: a run whose snapshot says 0.5 must
    write count_values bucketed at 0.5, not at the flat baseline."""
    written = {}

    async def _capture_result(_db, *, brainstorm_request_id, status, count_values, error):
        written["count_values"] = json.loads(count_values)

    async def _row(_db, _request_id):
        return _Row(_params(0.5))

    message = {
        "private_id": 1,
        "result": {
            "success": True,
            "duration_seconds": 1.0,
            "scorecards": {
                # Banded at the 0.02 baseline, unverified at this run's 0.5.
                "1": _card("obs_a", 0.3).model_dump(),
            },
        },
    }

    with (
        patch(f"{_CONSUMER}.db_session", new=_fake_db_session),
        patch(f"{_CONSUMER}.select_brainstorm_request_by_id_on_db", new=_row),
        patch(f"{_CONSUMER}.update_brainstorm_request_result_by_id_on_db", new=_capture_result),
        patch(f"{_CONSUMER}.upsert_observer_whitelist_on_db", new=AsyncMock()),
        patch(f"{_CONSUMER}.update_last_time_calculated_graperank_on_db", new=AsyncMock()),
    ):
        asyncio.run(process_message(message))

    assert written["count_values"]["medium_high"] == {}
    assert written["count_values"]["low"] == {"1": 1}
