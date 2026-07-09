"""Behavioral tests for priority-weighted admission (cross-lane fairness).

`choose_admission_lane` picks which priority lane fills the next admission slot,
given the currently-overdue lanes and how many have been admitted per lane so
far. Weight per lane is p+1, so over many slots the admitted mix approximates
the weights (lane 1 : lane 0 -> 2:1), and no lane is fully starved.
"""

from app.services.scheduler import choose_admission_lane


def test_single_active_lane_is_chosen():
    assert choose_admission_lane([2], admitted_counts={}) == 2


def test_empty_history_prefers_highest_lane():
    # No admissions yet -> the highest-priority lane goes first.
    assert choose_admission_lane([0, 1, 2], admitted_counts={}) == 2


def test_low_lane_catches_up_after_high_lane_admitted():
    # Lane 1 already got one; lane 0 is now furthest below its share.
    assert choose_admission_lane([0, 1], admitted_counts={1: 1}) == 0


def test_tie_breaks_to_higher_lane():
    # Both on their exact target share (1/3, 2/3) -> higher lane wins the tie.
    assert choose_admission_lane([0, 1], admitted_counts={0: 1, 1: 2}) == 1


def _simulate(active, slots):
    admitted: dict[int, int] = {}
    for _ in range(slots):
        p = choose_admission_lane(active, admitted)
        admitted[p] = admitted.get(p, 0) + 1
    return admitted


def test_two_lanes_approximate_2_to_1():
    admitted = _simulate([0, 1], 300)
    ratio = admitted[1] / admitted[0]  # weights 2:1
    assert 1.8 <= ratio <= 2.2


def test_three_lanes_approximate_weights():
    admitted = _simulate([0, 1, 2], 600)  # weights 1:2:3
    assert abs(admitted[1] / admitted[0] - 2.0) < 0.3
    assert abs(admitted[2] / admitted[0] - 3.0) < 0.3
