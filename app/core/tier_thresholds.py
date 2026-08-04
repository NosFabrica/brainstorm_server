"""Canonical tier bands and the tier classifier.

Each TIER_* constant is the LOWER bound of the tier it names. The bands are
fixed — no endpoint accepts overrides. Bucket names match the GR result
writer's `count_values` keys.

DEFAULT_VERIFIED_THRESHOLD is NOT the live verified line: that is per-
relationship and comes from the observer's saved preset (see
app.services.verified_cutoffs). It equals DEFAULT's seeded follower cutoff and
survives only as a fallback, for when a preset can't be read
(`FALLBACK_VERIFIED_CUTOFFS`) or a run has no params snapshot
(`verified_line_for_run`).
"""

TIER_HIGH = 0.50
TIER_MEDIUM_HIGH = 0.20
TIER_MEDIUM = 0.07
DEFAULT_VERIFIED_THRESHOLD = 0.02

FLAGGED_TIER = "low_and_reported_by_2_or_more_trusted_pubkeys"

# Cypher CASE arm order (`user_repo._TIER_PREDICATES`); also the `count_values` keys.
TIER_NAMES: tuple[str, ...] = (
    "high",
    "medium_high",
    "medium",
    "medium_low",
    "low",
    FLAGGED_TIER,
)


def classify_tier(
    influence: float | None,
    trusted_reporters: int,
    verified_line: float,
) -> str:
    """A subject's tier bucket.

    Bucketing lives here as well as in Cypher (`user_repo._TIER_PREDICATES`)
    because the GrapeRank result writer buckets scorecards that aren't in the
    graph yet. Keep the two in step —
    tests/integration/test_tier_classifier_matches_cypher.py fails otherwise.

    Fallthrough: bands apply only strictly above `verified_line`; at or below it
    a subject is `low`, or flagged with 2+ trusted reporters. No influence → low.
    """
    if influence is None:
        return "low"
    if influence > verified_line:
        if influence >= TIER_HIGH:
            return "high"
        if influence >= TIER_MEDIUM_HIGH:
            return "medium_high"
        if influence >= TIER_MEDIUM:
            return "medium"
        return "medium_low"
    return FLAGGED_TIER if trusted_reporters >= 2 else "low"
