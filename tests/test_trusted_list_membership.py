"""Membership predicate + ordering (AC8) and polarity bucketing (AC7)."""
from __future__ import annotations

import pytest

from app.services.tagging_parse import is_applied, is_disputed, is_neutral
from app.services.trusted_list_build import compute_members

P1 = "1" * 64
P2 = "2" * 64
P3 = "3" * 64


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
    taggings = [
        (P1, 1.0), (P1, 1.0),              # 2 applies, 0 disputes -> member
        (P2, 1.0), (P2, -1.0),             # 1 apply, 1 dispute -> tied, excluded
        (P3, 1.0), (P3, 1.0), (P3, -1.0),  # 2 applies, 1 dispute -> member
    ]
    members = {m.pubkey for m in compute_members(taggings, cutoff=1)}
    assert members == {P1, P3}


def test_cutoff_is_an_inclusive_floor():
    taggings = [(P1, 1.0), (P1, 1.0)]
    assert compute_members(taggings, cutoff=2) != []   # applications == cutoff
    assert compute_members(taggings, cutoff=3) == []


def test_neutral_polarity_counts_as_neither():
    # Two neutral assertions must not make P1 a member, nor a disputer.
    assert compute_members([(P1, 0.0), (P1, 0.2)], cutoff=1) == []


def test_members_ordered_by_applications_then_pubkey():
    taggings = [
        (P3, 1.0),
        (P1, 1.0), (P1, 1.0),
        (P2, 1.0), (P2, 1.0),
    ]
    members = compute_members(taggings, cutoff=1)
    # applications desc, then pubkey asc — stable so an unchanged membership
    # republishes byte-identically instead of churning the relay.
    assert [m.pubkey for m in members] == [P1, P2, P3]
    assert members[0].applications == 2
    assert members[2].applications == 1


def test_counts_are_reported_per_member():
    members = compute_members([(P1, 1.0), (P1, 1.0), (P1, -1.0)], cutoff=1)
    assert members[0].applications == 2
    assert members[0].disputes == 1


def test_empty_input_yields_no_members():
    assert compute_members([], cutoff=1) == []
