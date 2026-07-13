"""Recompute the user-only report graph from relay events (backfill core).

Pure functions: the relay's kind-1984 reports (minus valid same-author k=1984
kind-5 deletions) define the desired `reported_by` state; `diff_reported_by`
turns desired-vs-current into a member-level reconcile plan. No I/O here.
"""
from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from typing import Iterable, Mapping

from app.message_queue_tasks.process_strfry_event import _extract_report_targets


def _deletions_by_report_id(deletion_events: Iterable[dict]) -> dict[str, set[str]]:
    """report_id -> set of kind-5 authors that requested its deletion.

    Only kind-5 events carrying at least one `k=1984` tag count as report
    deletions (NIP-09 `k` is a SHOULD; we require it to scope the sweep). The
    author is tracked so `build_desired_reported_by` can enforce NIP-09
    author-match — a deletion only removes the deleter's own report.
    """
    out: dict[str, set[str]] = defaultdict(set)
    for d in deletion_events:
        tags = d.get("tags", [])
        if not any(t and t[0] == "k" and len(t) > 1 and t[1] == "1984" for t in tags):
            continue
        author = d.get("pubkey")
        if not isinstance(author, str):
            continue
        for t in tags:
            if t and t[0] == "e" and len(t) > 1:
                out[t[1]].add(author)
    return out


def build_desired_reported_by(
    report_events: Iterable[dict],
    deletion_events: Iterable[dict] = (),
) -> dict[str, set[str]]:
    """Desired `reported_by` state: target pubkey -> set of reporter pubkeys.

    Only user-level reports count (see `_extract_report_targets`), and a report
    is dropped if a same-author `k=1984` kind-5 deletion references its id.
    """
    deleters = _deletions_by_report_id(deletion_events)
    desired: dict[str, set[str]] = defaultdict(set)
    for ev in report_events:
        reporter = ev.get("pubkey")
        if not isinstance(reporter, str):
            continue
        eid = ev.get("id")
        if isinstance(eid, str) and reporter in deleters.get(eid, set()):
            continue  # same-author deletion -> report retracted
        for target in _extract_report_targets(ev):
            desired[target].add(reporter)
    return dict(desired)


@dataclass(frozen=True)
class ReportedByDiff:
    """Member-level reconcile for the `reported_by:` sets. Never a whole-key
    delete — only SADD/SREM of individual reporters, so it is safe to apply live
    (a concurrent ingest SADD is not clobbered)."""

    to_add: dict[str, set[str]]  # target -> reporters to SADD
    to_remove: dict[str, set[str]]  # target -> reporters to SREM

    @property
    def add_count(self) -> int:
        return sum(len(v) for v in self.to_add.values())

    @property
    def remove_count(self) -> int:
        return sum(len(v) for v in self.to_remove.values())


def diff_reported_by(
    desired: Mapping[str, set[str]],
    current: Mapping[str, set[str]],
) -> ReportedByDiff:
    """Per-target SADD/SREM to move `current` reported_by state to `desired`."""
    to_add: dict[str, set[str]] = {}
    to_remove: dict[str, set[str]] = {}
    for target in set(desired) | set(current):
        want = desired.get(target, set())
        have = current.get(target, set())
        if add := want - have:
            to_add[target] = add
        if remove := have - want:
            to_remove[target] = remove
    return ReportedByDiff(to_add=to_add, to_remove=to_remove)
