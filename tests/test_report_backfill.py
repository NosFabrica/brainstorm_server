"""Backfill of the user-only report graph, recomputed from relay events.

The pure core: given the relay's kind-1984 (reports) and kind-5 (deletions with a
`k=1984` tag) events, compute the desired `reported_by` state (target -> reporters),
and diff it against the current state to a reconcile plan. I/O (strfry scan, Neo4j,
Redis) lives in the thin CLI and is verified per-env, not here.
"""
from __future__ import annotations

from app.services.report_backfill_service import (
    build_desired_reported_by,
    diff_reported_by,
)


def _report(reporter: str, targets: list[str], eid: str = "id0") -> dict:
    return {
        "id": eid,
        "pubkey": reporter,
        "kind": 1984,
        "tags": [["p", t] for t in targets],
        "content": "",
    }


def _deletion(author: str, report_ids: list[str], k: str | None = "1984") -> dict:
    tags = [["e", rid] for rid in report_ids]
    if k is not None:
        tags.append(["k", k])
    return {"id": "del", "pubkey": author, "kind": 5, "tags": tags, "content": ""}


def test_user_report_contributes_target_to_reporter():
    ev = _report(reporter="r" * 64, targets=["a" * 64])
    assert build_desired_reported_by([ev]) == {"a" * 64: {"r" * 64}}


def test_note_report_contributes_nothing():
    ev = _report(reporter="r" * 64, targets=["a" * 64])
    ev["tags"].insert(0, ["e", "n" * 64])  # now targets a note, not the user
    assert build_desired_reported_by([ev]) == {}


def test_same_author_k1984_deletion_excludes_the_report():
    r = _report(reporter="r" * 64, targets=["a" * 64], eid="rep1")
    d = _deletion(author="r" * 64, report_ids=["rep1"])  # same author, k=1984
    assert build_desired_reported_by([r], [d]) == {}


def test_cross_author_deletion_does_not_exclude_the_report():
    # NIP-09 author-match: a deletion signed by someone else can't drop the report.
    r = _report(reporter="r" * 64, targets=["a" * 64], eid="rep1")
    forged = _deletion(author="x" * 64, report_ids=["rep1"])
    assert build_desired_reported_by([r], [forged]) == {"a" * 64: {"r" * 64}}


def test_kind5_without_k1984_tag_is_ignored():
    # Only report-scoped deletions (k=1984) sweep reports; a bare kind-5 doesn't.
    r = _report(reporter="r" * 64, targets=["a" * 64], eid="rep1")
    d = _deletion(author="r" * 64, report_ids=["rep1"], k=None)
    assert build_desired_reported_by([r], [d]) == {"a" * 64: {"r" * 64}}


def test_deleting_one_of_two_reports_keeps_the_surviving_edge():
    # Same reporter->target via two events; deleting one leaves the member.
    r1 = _report(reporter="r" * 64, targets=["a" * 64], eid="rep1")
    r2 = _report(reporter="r" * 64, targets=["a" * 64], eid="rep2")
    d = _deletion(author="r" * 64, report_ids=["rep1"])
    assert build_desired_reported_by([r1, r2], [d]) == {"a" * 64: {"r" * 64}}


# --- diff_reported_by: desired-vs-current -> member-level reconcile -------------


def test_stale_member_is_scheduled_for_removal():
    # `n` reported `a` via a note report / deleted report -> not in desired.
    current = {"a" * 64: {"r" * 64, "n" * 64}}
    desired = {"a" * 64: {"r" * 64}}
    diff = diff_reported_by(desired, current)
    assert diff.to_remove == {"a" * 64: {"n" * 64}}
    assert diff.to_add == {}


def test_missing_member_is_scheduled_for_addition():
    current = {"a" * 64: {"r" * 64}}
    desired = {"a" * 64: {"r" * 64, "q" * 64}}
    diff = diff_reported_by(desired, current)
    assert diff.to_add == {"a" * 64: {"q" * 64}}
    assert diff.to_remove == {}


def test_matching_state_yields_empty_diff():
    state = {"a" * 64: {"r" * 64}, "b" * 64: {"r" * 64}}
    diff = diff_reported_by(state, state)
    assert diff.to_add == {} and diff.to_remove == {}
    assert diff.add_count == 0 and diff.remove_count == 0


def test_target_absent_from_desired_removes_every_member():
    # A target whose reports were all note-reports/deleted: strip all members,
    # member-by-member (never a whole-key DEL that could race a live SADD).
    current = {"a" * 64: {"r" * 64, "q" * 64}}
    diff = diff_reported_by({}, current)
    assert diff.to_remove == {"a" * 64: {"r" * 64, "q" * 64}}
    assert diff.remove_count == 2
