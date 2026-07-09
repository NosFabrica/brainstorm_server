"""Per-sink forced-full override + every-Nth backstop decisions (drift repair).

Pure decision logic for published-state-drift repair: per-sink full-vs-delta
resolution, the every-Nth scheduled backstop, its counter, and the admin resync
target mapping. No DB/queue — those are wired on top of these.
"""

import pytest

from app.services.publish_drift import (
    backstop_due,
    resolve_full_sync,
    resync_target_to_flags,
)


def test_resolve_full_sync_is_full_when_setting_or_force_full():
    # setting drives it (the env default), force_full overrides per run, and a
    # nullable force_full of None means "no override".
    assert resolve_full_sync(setting_full=True, force_full=None) is True
    assert resolve_full_sync(setting_full=False, force_full=True) is True
    assert resolve_full_sync(setting_full=False, force_full=None) is False
    assert resolve_full_sync(setting_full=False, force_full=False) is False


def test_backstop_due_on_the_nth_run_counting_the_upcoming_one():
    # every_n=5: runs_since_full counts completed scheduled deltas since the last
    # full. The upcoming run is the (runs_since_full + 1)th — due on the 5th.
    assert [backstop_due(n, every_n=5) for n in range(6)] == [
        False,  # 0 → upcoming is 1st
        False,  # 1 → 2nd
        False,  # 2 → 3rd
        False,  # 3 → 4th
        True,  # 4 → 5th  ← due
        True,  # 5 → already overdue (e.g. prior full failed)
    ]


def test_backstop_disabled_when_every_n_non_positive():
    assert backstop_due(100, every_n=0) is False
    assert backstop_due(100, every_n=-1) is False


def test_resync_target_maps_to_per_sink_force_flags():
    # (force_full_relay, force_full_vespa)
    assert resync_target_to_flags("relay") == (True, False)
    assert resync_target_to_flags("vespa") == (False, True)
    assert resync_target_to_flags("both") == (True, True)


def test_resync_target_rejects_unknown_value():
    with pytest.raises(ValueError):
        resync_target_to_flags("everything")
