"""The user-only report graph, recomputed from relay events. Pure; no I/O.

Shared by all three report paths: live kind-1984 ingest, the whole-graph backfill
(`scripts/backfill_user_reports.py`), and the live kind-5 recompute
(`process_event_kind_5`). See app/services/CLAUDE.md.
"""
from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from typing import Iterable, Mapping


def extract_report_targets(event: dict) -> list[str]:
    """Reported target pubkeys for a kind-1984 event, or [] if it targets a note.

    NIP-56: a report against a note/media carries an `e` tag (profile reports are
    `p`-only). We only count user-level reports, so any `e` tag -> no targets.
    """
    tags = event.get("tags", [])
    if any(tag and tag[0] == "e" for tag in tags):
        return []
    return [tag[1] for tag in tags if tag and tag[0] == "p" and len(tag) > 1]


def _deletions_by_report_id(
    deletion_events: Iterable[dict], report_ids: set[str]
) -> dict[str, set[str]]:
    """report_id -> set of kind-5 authors that requested its deletion.

    An `e` naming one of our report ids, plus the caller's author-match, is what
    validates a retraction. Restricting to `report_ids` bounds this map to
    |reports| however many unrelated deletions the relay holds.
    """
    out: dict[str, set[str]] = defaultdict(set)
    for d in deletion_events:
        author = d.get("pubkey")
        if not isinstance(author, str):
            continue
        for t in d.get("tags", []):
            if t and t[0] == "e" and len(t) > 1 and t[1] in report_ids:
                out[t[1]].add(author)
    return out


def build_desired_reported_by(
    report_events: Iterable[dict],
    deletion_events: Iterable[dict] = (),
) -> dict[str, set[str]]:
    """Desired `reported_by` state: target pubkey -> set of reporter pubkeys.

    Only user-level reports count (see `extract_report_targets`), and a report is
    dropped if a same-author kind-5 references its id.
    """
    reports = [ev for ev in report_events if isinstance(ev.get("pubkey"), str)]

    # Deletions are the live kind-5 path's problem; the backfill only needs them
    # for reports the relay somehow still holds. Skip the id set when there are
    # none — `surviving_report_targets` calls this per kind-5.
    deleters: dict[str, set[str]] = {}
    deletions = list(deletion_events)
    if deletions:
        report_ids = {ev["id"] for ev in reports if isinstance(ev.get("id"), str)}
        deleters = _deletions_by_report_id(deletions, report_ids)

    desired: dict[str, set[str]] = defaultdict(set)
    for ev in reports:
        reporter = ev["pubkey"]
        eid = ev.get("id")
        if isinstance(eid, str) and reporter in deleters.get(eid, set()):
            continue  # same-author deletion -> report retracted
        for target in extract_report_targets(ev):
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


# --- live kind-5: author-scoped recompute --------------------------------------


def surviving_report_targets(report_events: Iterable[dict], author: str) -> set[str]:
    """Targets `author` still user-reports, given their surviving relay events.

    No deletions to subtract: strfry purges the retracted report before the
    kind-5 reaches us, so what the relay still holds IS the surviving set.
    """
    by_target = build_desired_reported_by(
        ev for ev in report_events if ev.get("pubkey") == author
    )
    return {target for target, reporters in by_target.items() if author in reporters}


@dataclass(frozen=True)
class AuthorReportDiff:
    """One author's outgoing `REPORTS` reconcile: which targets to link/unlink."""

    to_add: set[str]
    to_remove: set[str]

    def __bool__(self) -> bool:
        """False when the graph already matches — the common case, worth skipping."""
        return bool(self.to_add or self.to_remove)


def diff_author_targets(desired: set[str], current: set[str]) -> AuthorReportDiff:
    """Targets to link/unlink to move `author`'s outgoing REPORTS to `desired`."""
    return AuthorReportDiff(to_add=desired - current, to_remove=current - desired)
