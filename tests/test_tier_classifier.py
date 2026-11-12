from app.core.tier_thresholds import FLAGGED_TIER, classify_tier

LINE = 0.02


def test_no_stored_influence_reported_by_two_trusted_is_flagged():
    # Out of reach, or at or below the persist floor: still flagged when reported.
    assert classify_tier(None, 2, LINE) == FLAGGED_TIER


def test_no_stored_influence_without_reports_is_low():
    assert classify_tier(None, 1, LINE) == "low"


def test_on_the_line_reported_is_flagged_and_above_is_banded():
    assert classify_tier(LINE, 2, LINE) == FLAGGED_TIER
    assert classify_tier(0.03, 5, LINE) == "medium_low"
