"""Admin reconcile: diff a sink's actual published state against the Neo4j
desired state and classify the corrections (pure).

Streaming and memory-bounded: the classifier holds only the desired map and the
(small) correction set — actual events/docs are consumed and dropped, never
buffered. The relay stream, Vespa GET-loop, Neo4j batch read, apply, and endpoint
wire these decisions to I/O.
"""

from typing import Iterable, NamedTuple


def build_desired_map(
    rows: Iterable[tuple[str, float | None]], cutoff: float
) -> dict[str, int]:
    """The observer's desired published state from Neo4j `(observee, influence)`
    rows: `{observee: rank}` for the above-cutoff set, where `rank =
    round(influence * 100)`. Rows with null influence (property unset) are
    skipped, matching the publish path's filter."""
    desired: dict[str, int] = {}
    for observee, influence in rows:
        if influence is None:
            continue
        if round(influence, 2) >= cutoff:
            desired[observee] = round(influence * 100)
    return desired


class RelayDrift(NamedTuple):
    """Corrections to bring the relay in line with the desired state.
    `missing`/`stale` carry the DESIRED rank to republish; `ghost` is the
    Observee whose TA should be deleted."""

    missing: list[tuple[str, int]]
    stale: list[tuple[str, int]]
    ghost: list[str]


class RelayDriftAccumulator:
    """Push-style relay drift classifier: `observe(observee, rank)` per streamed
    event (the event is then dropped), `result()` after the stream ends.

    Memory = the desired map (popped as matched) + the small correction lists, so
    a subscription can stream a 134k-TA observer past it without buffering the
    actual events. `classify_relay_drift` is the pull-style wrapper over this."""

    def __init__(self, desired: dict[str, int]) -> None:
        self._remaining = dict(desired)
        self._seen: set[str] = set()
        self._stale: list[tuple[str, int]] = []
        self._ghost: list[str] = []

    def observe(self, observee: str, rank: int) -> None:
        if observee in self._seen:
            return  # the relay re-streamed an already-classified coordinate
        self._seen.add(observee)
        if observee not in self._remaining:
            self._ghost.append(observee)
            return
        expected = self._remaining.pop(observee)
        if expected != rank:
            self._stale.append((observee, expected))

    def result(self) -> RelayDrift:
        """Leftover desired entries are the missing set."""
        return RelayDrift(
            missing=list(self._remaining.items()),
            stale=self._stale,
            ghost=self._ghost,
        )


def classify_relay_drift(
    desired: dict[str, int], actual: Iterable[tuple[str, int]]
) -> RelayDrift:
    """Pull-style convenience over `RelayDriftAccumulator`: diff an iterable of
    actual TAs `(observee, rank)` against `desired` `{observee: rank}`."""
    acc = RelayDriftAccumulator(desired)
    for observee, rank in actual:
        acc.observe(observee, rank)
    return acc.result()


def summarize_drift(
    missing: list[str],
    stale: list[str],
    ghost: list[str],
    limit: int = 100,
    full: bool = False,
) -> dict:
    """Per-sink drift report: category counts plus the mismatched Observees
    (capped at `limit` unless `full`). Each list is the Observees in that
    category; `total` is the combined count, `truncated` flags an elided sample."""
    counts = {"missing": len(missing), "stale": len(stale), "ghost": len(ghost)}
    mismatches = [
        {"observee": observee, "kind": kind}
        for kind, observees in (
            ("missing", missing),
            ("stale", stale),
            ("ghost", ghost),
        )
        for observee in observees
    ]
    total = len(mismatches)
    return {
        "counts": counts,
        "total": total,
        "mismatches": mismatches if full else mismatches[:limit],
        "truncated": (not full) and total > limit,
    }


def classify_vespa_cell(expected_rank: int, actual_cell: int | None) -> str:
    """Classify a desired Observee's Vespa tensor cell for this observer:
    absent → "missing", present-but-different → "stale", equal → "match"."""
    if actual_cell is None:
        return "missing"
    if actual_cell != expected_rank:
        return "stale"
    return "match"
