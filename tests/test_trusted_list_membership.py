"""Membership predicate + ordering (AC8), polarity bucketing (AC7), and the
weighted-certainty scoring of D12."""
from __future__ import annotations

import pytest

from app.services.tagging_parse import is_applied, is_disputed, is_neutral
from app.services.trusted_list_build import compute_members, compute_score

P1 = "1" * 64
P2 = "2" * 64
P3 = "3" * 64

# Equal weight for every asserter, so the count-era assertions below still say
# what they meant to say: with uniform weights, more applications is a strictly
# higher score, and the predicate reduces to the v1 one.
W = 0.5


def uniform(pairs: list[tuple[str, float]]) -> list[tuple[str, float, float]]:
    """`(target, polarity)` pairs at a single shared weight."""
    return [(target, polarity, W) for target, polarity in pairs]


@pytest.mark.parametrize(
    "polarity,applied,disputed,neutral",
    [
        (1.0, True, False, False),
        (0.5, True, False, False),    # inclusive boundary
        (-1.0, False, True, False),
        (-0.5, False, True, False),   # inclusive boundary
        (0.0, False, False, True),    # reserved interval
        (0.4, False, False, True),
        (-0.4, False, False, True),
    ],
)
def test_polarity_bucketize(polarity, applied, disputed, neutral):
    assert is_applied(polarity) is applied
    assert is_disputed(polarity) is disputed
    assert is_neutral(polarity) is neutral


def test_membership_requires_cutoff_and_net_positive():
    taggings = uniform([
        (P1, 1.0), (P1, 1.0),              # 2 applies, 0 disputes -> member
        (P2, 1.0), (P2, -1.0),             # 1 apply, 1 dispute -> tied, excluded
        (P3, 1.0), (P3, 1.0), (P3, -1.0),  # 2 applies, 1 dispute -> member
    ])
    members = {m.pubkey for m in compute_members(taggings, cutoff=1)}
    assert members == {P1, P3}


def test_cutoff_is_an_inclusive_floor():
    taggings = uniform([(P1, 1.0), (P1, 1.0)])
    assert compute_members(taggings, cutoff=2) != []   # applications == cutoff
    assert compute_members(taggings, cutoff=3) == []


def test_neutral_polarity_counts_as_neither():
    # Two neutral assertions must not make P1 a member, nor a disputer, and
    # must contribute no weight.
    assert compute_members(uniform([(P1, 0.0), (P1, 0.2)]), cutoff=1) == []


def test_members_ordered_by_score_then_pubkey():
    taggings = uniform([
        (P3, 1.0),
        (P1, 1.0), (P1, 1.0),
        (P2, 1.0), (P2, 1.0),
    ])
    members = compute_members(taggings, cutoff=1)
    # score desc, then pubkey asc — stable so an unchanged membership
    # republishes byte-identically instead of churning the relay.
    assert [m.pubkey for m in members] == [P1, P2, P3]
    assert members[0].score == members[1].score > members[2].score


def test_mass_beats_count():
    """The whole point of D12: two heavy appliers outrank ten light ones."""
    light = compute_members([(P1, 1.0, 0.03)] * 10, cutoff=1)
    heavy = compute_members([(P2, 1.0, 0.90)] * 2, cutoff=1)
    assert heavy[0].score > light[0].score


def test_counts_are_reported_per_member():
    members = compute_members(uniform([(P1, 1.0), (P1, 1.0), (P1, -1.0)]), cutoff=1)
    assert members[0].applications == 2
    assert members[0].disputes == 1


def test_empty_input_yields_no_members():
    assert compute_members([], cutoff=1) == []


def test_zero_weight_asserters_score_out():
    """An unscored asserter contributes no mass, so the pair rounds to 0 and
    drops — it must not divide by zero or slip through on count alone."""
    assert compute_members([(P1, 1.0, 0.0)] * 3, cutoff=1) == []


def test_dispute_heavy_pair_is_excluded_even_when_applications_lead():
    """Three clauses, not two. Applications outnumber disputes, so the v1
    predicate passes, but the dispute carries the mass — clamped to 0, dropped.
    """
    taggings = [(P1, 1.0, 0.03), (P1, 1.0, 0.03), (P1, -1.0, 0.90)]
    assert compute_members(taggings, cutoff=1) == []


# --- parity with tapestry -------------------------------------------------
#
# The six vectors validated live against tapestry's own implementation
# (scripts/tl-ladder-validate.js). Weights here are `rank / 100`, which is how
# tapestry derives them. If any of these move, the two estates have forked and
# a TL published by one will not mean the same thing when read by the other.


@pytest.mark.parametrize(
    "name,taggings,expected",
    [
        ("one rank-100 apply", [(P1, 1.0, 1.00)], 50),
        ("ten rank-3 applies", [(P1, 1.0, 0.03)] * 10, 19),
        ("two rank-90 applies", [(P1, 1.0, 0.90)] * 2, 71),
        (
            "2x rank-40 applies + one rank-40 dispute",
            [(P1, 1.0, 0.40), (P1, 1.0, 0.40), (P1, -1.0, 0.40)],
            19,
        ),
        (
            "equal-weight split: 2x rank-40 vs one rank-80",
            [(P1, 1.0, 0.40), (P1, 1.0, 0.40), (P1, -1.0, 0.80)],
            None,  # excluded
        ),
        (
            "2x rank-3 applies vs one rank-90 dispute",
            [(P1, 1.0, 0.03), (P1, 1.0, 0.03), (P1, -1.0, 0.90)],
            None,  # excluded
        ),
    ],
)
def test_tapestry_parity_vectors(name, taggings, expected):
    members = compute_members(taggings, cutoff=1)
    if expected is None:
        assert members == [], f"{name}: expected exclusion"
    else:
        assert members[0].score == expected, name


def test_score_rounds_half_up_like_javascript():
    """Lands exactly on 0.5 before rounding. Python's `round` is banker's
    rounding and returns 0; JS `Math.round` returns 1, and the wire has to
    agree with tapestry."""
    # average 0.01 x certainty 0.5 = 0.005 -> 0.5 on the wire quantum.
    assert compute_score(0.01, 1.0) == 1
    assert round(0.5) == 0  # the trap this guards against

# --- dispute-stress supplement -------------------------------------------
#
# Computed by tapestry against its own fold, with operations identical to ours
# (quantize rank/100 first, accumulate in doubles, then round). These stress
# the dispute side, which the six headline vectors barely touch.
#
# G and N are the important ones: BOTH have a passing score (44 and 39) and
# BOTH must still be excluded, because the predicate's middle clause
# (`applications > disputes`) runs before the score filter. A two-clause
# implementation publishes them. That is the only cheap way to catch a
# regression from three clauses back to two, so do not "simplify" this test
# by dropping the membership assertion and keeping the score one.


@pytest.mark.parametrize(
    "name,rank_votes,expected_score,expected_member",
    [
        ("G: one heavy apply vs one light dispute", [(90, 1), (3, -1)], 44, False),
        ("N: two applies vs two disputes", [(40, 1), (40, 1), (3, -1), (3, -1)], 39, False),
        ("H: high-trust dispute buries five light applies",
         [(10, 1)] * 5 + [(90, -1)], 0, False),
        ("I: low-trust dispute barely dents two heavy applies",
         [(90, 1), (90, 1), (3, -1)], 70, True),
        ("J: mid-trust dispute really discounts",
         [(50, 1), (50, 1), (30, -1)], 32, True),
        ("K: a single rank-3 apply still scores", [(3, 1)], 2, True),
        ("M: two rank-3 applies", [(3, 1), (3, 1)], 4, True),
        # Raw 0.6907... rounds UP to 1, which is the score>=1 membership
        # boundary: banker's rounding would exclude this member.
        ("L: a single rank-1 apply lands exactly on the floor", [(1, 1)], 1, True),
    ],
)
def test_dispute_stress_vectors(name, rank_votes, expected_score, expected_member):
    taggings = [(P1, float(polarity), rank / 100) for rank, polarity in rank_votes]

    weighted_input = sum(w for _, _, w in taggings)
    weighted_sum = sum(w if p > 0 else -w for _, p, w in taggings)
    assert compute_score(weighted_sum, weighted_input) == expected_score, name

    members = compute_members(taggings, cutoff=1)
    assert bool(members) is expected_member, name
    if expected_member:
        assert members[0].score == expected_score, name
