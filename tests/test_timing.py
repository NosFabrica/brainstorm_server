"""Reusable per-segment wall-clock timing helper."""

from app.utils.timing import timed


def test_timed_records_segment_elapsed_seconds():
    timings: dict[str, float] = {}

    with timed(timings, "work"):
        pass

    assert isinstance(timings["work"], float)
    assert timings["work"] >= 0


def test_timed_records_even_when_the_block_raises():
    # The segment is recorded in a finally, so a failed segment is still
    # attributed (the publish path logs timings on the failure path too).
    timings: dict[str, float] = {}

    try:
        with timed(timings, "boom"):
            raise ValueError("nope")
    except ValueError:
        pass

    assert "boom" in timings
