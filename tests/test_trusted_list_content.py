"""Trusted Lists of tagged content: membership, wire shape, titles (story 2,
AC9-AC12, AC17; ADR `trusted-lists/0002` D3-D5, D7).

`plan_content_lists` is pure over the event-tagging rows and what the lookups
found, so the whole content dictionary is exercised here without a backend.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from app.repos.event_tagging_repo import EventTaggingRow, TaggedEvent
from app.services.trusted_list_build import (
    KIND_NAMES,
    Member,
    build_content_list_content,
    build_content_list_tags,
    build_trusted_list_content,
    build_trusted_list_tags,
    compute_content_d_tag,
    compute_members,
)
from app.services.trusted_list_service import TargetView, plan_content_lists

OBSERVER = "a" * 64
TAG_AUTHOR = "d" * 64
CURATOR = "c" * 64
BOOKS = f"39998:{CURATOR}:books"
HEADERS = {
    BOOKS: TaggedEvent(BOOKS, 39998, CURATOR, "books", (), ("Book", "Books")),
}


def row(target, *, slot="e", asserter="1" * 64, polarity=1.0, created_at=1, slug="espresso",
        name="espresso", tag_author=TAG_AUTHOR):
    return EventTaggingRow(
        asserter_pubkey=asserter,
        target=target,
        target_slot=slot,
        target_relay=None,
        polarity=polarity,
        created_at_unix=created_at,
        tag_event_id=f"{tag_author}{slug}",
        tag_author_pubkey=tag_author,
        slug=slug,
        name=name,
        description=f"About {name}",
    )


def note(id_, kind=1, **kw):
    return TaggedEvent(id_, kind, "e" * 64, kw.get("d"), tuple(kw.get("z", ())), ())


def plan(rows, view, weights=None, *, cutoff=1, rigor=0.5, min_uses=1):
    weights = weights or {r.asserter_pubkey: 0.5 for r in rows}
    return plan_content_lists(
        observer=OBSERVER,
        rows=rows,
        view=view,
        weights=weights,
        cutoff=cutoff,
        rigor=rigor,
        min_uses=min_uses,
    )


# --- U9: membership is story 1's predicate (AC10) ---------------------------


@pytest.mark.parametrize(
    "stances, cutoff, member",
    [
        ([(1.0, 0.5)], 1, True),
        ([(1.0, 0.5)], 2, False),  # applications < cutoff
        ([(1.0, 0.5), (-1.0, 0.5)], 1, False),  # applications == disputes
        ([(1.0, 0.005)], 1, False),  # weighted score rounds below 1
    ],
    ids=["member", "under cutoff", "even split", "score below 1"],
)
def test_content_membership_uses_the_people_predicate(stances, cutoff, member):
    rows = [
        row("2" * 64, asserter=f"{n}" * 64, polarity=p) for n, (p, _) in enumerate(stances, 1)
    ]
    weights = {f"{n}" * 64: w for n, (_, w) in enumerate(stances, 1)}
    planned = plan(rows, TargetView(by_id={"2" * 64: note("2" * 64)}), weights, cutoff=cutoff)
    assert [m.pubkey for m in planned.lists[0].members] == (["2" * 64] if member else [])
    # And the score is exactly story 1's, for the same (target, polarity, weight).
    expected = compute_members(
        [("2" * 64, p, w) for p, w in stances], cutoff=cutoff, rigor=0.5
    )
    assert planned.lists[0].members == expected


def test_rigor_is_the_observers():
    rows = [row("2" * 64)]
    view = TargetView(by_id={"2" * 64: note("2" * 64)})
    loose = plan(rows, view, rigor=0.3).lists[0].members[0].score
    strict = plan(rows, view, rigor=0.65).lists[0].members[0].score
    assert loose > strict


# --- U10: one target, one stance per asserter (AC10) -------------------------


def test_id_and_coordinate_taggings_count_as_one_target():
    """Two asserters, one by id (legacy) and one by coordinate: one member."""
    item = f"30023:{'e' * 64}:essay"
    view = TargetView(by_id={"3" * 64: note("3" * 64, kind=30023, d="essay")})
    rows = [row("3" * 64, asserter="1" * 64), row(item, slot="a", asserter="2" * 64)]
    lists = plan(rows, view).lists
    assert len(lists) == 1
    assert [(m.pubkey, m.applications) for m in lists[0].members] == [(item, 2)]


def test_one_stance_per_asserter_newest_wins():
    item = f"30023:{'e' * 64}:essay"
    view = TargetView(by_id={"3" * 64: note("3" * 64, kind=30023, d="essay")})
    rows = [
        row("3" * 64, asserter="1" * 64, polarity=1.0, created_at=1),
        row(item, slot="a", asserter="1" * 64, polarity=-1.0, created_at=2),
        row(item, slot="a", asserter="2" * 64, polarity=1.0, created_at=1),
    ]
    listed = plan(rows, view).lists[0]
    assert listed.taggings_considered == 2  # asserter 1 once, not twice
    assert listed.members == []  # one apply, one dispute: not a member


def test_content_members_ordered_by_score_then_key():
    rows = [
        row("5" * 64, asserter="1" * 64),
        row("4" * 64, asserter="1" * 64),
        row("6" * 64, asserter="1" * 64),
        row("6" * 64, asserter="2" * 64),
    ]
    view = TargetView(by_id={k * 64: note(k * 64) for k in "456"})
    members = plan(rows, view).lists[0].members
    assert [m.pubkey for m in members] == ["6" * 64, "4" * 64, "5" * 64]


def test_pair_below_min_uses_gets_no_list():
    view = TargetView(by_id={"2" * 64: note("2" * 64)})
    assert plan([row("2" * 64)], view, min_uses=2).lists == []
    rows = [row("2" * 64, asserter="1" * 64), row("2" * 64, asserter="2" * 64)]
    assert len(plan(rows, view, min_uses=2).lists) == 1


def test_a_neutral_stance_is_no_use():
    view = TargetView(by_id={"2" * 64: note("2" * 64)})
    assert plan([row("2" * 64, polarity=0.0)], view).lists == []


# --- U11: wire shape (AC11) ------------------------------------------------


@pytest.mark.parametrize(
    "list_kind, member_tag, member_field, key",
    [(30393, "e", "id", "2" * 64), (30394, "a", "address", f"30402:{'e' * 64}:bike")],
)
def test_content_list_wire_shape(list_kind, member_tag, member_field, key):
    members = [Member(pubkey=key, applications=2, disputes=1, score=42)]
    d_tag = compute_content_d_tag(list_kind, OBSERVER, TAG_AUTHOR, "espresso", kind=1)
    tags = build_content_list_tags(
        list_kind=list_kind,
        d_tag=d_tag,
        observer=OBSERVER,
        title="espresso Notes",
        description="About espresso",
        tag_author_pubkey=TAG_AUTHOR,
        slug="espresso",
        tagging_target="1",
        members=members,
        cutoff=1,
        min_rank=3,
        rigor=0.5,
    )
    assert tags == [
        ["d", d_tag],
        ["title", "espresso Notes"],
        ["description", "About espresso"],
        ["metric", "tag-membership"],
        ["observer", OBSERVER],
        ["source-tag", f"39999:{TAG_AUTHOR}:espresso", TAG_AUTHOR, "espresso"],
        ["tagging-target", "1"],
        ["cutoff", "1"],
        ["min-rank", "3"],
        ["rigor", "0.5"],
        [member_tag, key, "", "42"],
    ]
    content = json.loads(build_content_list_content(list_kind, members))
    assert content == {
        "members": [{member_field: key, "endorsements": 2, "disputes": 1, "score": 42}]
    }


def test_content_list_has_no_back_references():
    item = f"39999:{'e' * 64}:dune"
    tags = build_content_list_tags(
        list_kind=30394,
        d_tag="x",
        observer=OBSERVER,
        title="t",
        description="",
        tag_author_pubkey=TAG_AUTHOR,
        slug="espresso",
        tagging_target=BOOKS,
        members=[Member(pubkey=item, applications=1, disputes=0, score=10)],
        cutoff=1,
        min_rank=3,
    )
    assert [t for t in tags if t[0] in ("a", "p", "e")] == [["a", item, "", "10"]]


@pytest.mark.parametrize(
    "list_kind, target, expected",
    [
        (30393, dict(kind=1), "tl-tag-notes-aaaaaaaa-dddddddd-espresso-k1"),
        (30394, dict(kind=30402), "tl-tag-items-aaaaaaaa-dddddddd-espresso-k30402"),
        (
            30394,
            dict(dlist_author=CURATOR, dlist_key="books"),
            "tl-tag-items-aaaaaaaa-dddddddd-espresso-dlcccccccc-books",
        ),
    ],
    ids=["notes-k1", "items-k30402", "items-dl"],
)
def test_content_d_tag(list_kind, target, expected):
    assert compute_content_d_tag(list_kind, OBSERVER, TAG_AUTHOR, "espresso", **target) == expected


def test_same_named_tags_by_different_authors_get_distinct_lists():
    view = TargetView(by_id={"2" * 64: note("2" * 64)})
    rows = [row("2" * 64, tag_author="d" * 64), row("2" * 64, tag_author="b" * 64)]
    lists = plan(rows, view).lists
    assert [x.title for x in lists] == ["espresso Notes", "espresso Notes"]
    assert len({x.d_tag for x in lists}) == 2


# --- U12: titles (AC12) ------------------------------------------------------


def test_content_title_from_a_dlist():
    item = f"39999:{'e' * 64}:dune"
    view = TargetView(
        by_coordinate={item: TaggedEvent("1" * 64, 39999, "e" * 64, "dune", (BOOKS,), ())},
        headers=HEADERS,
    )
    (listed,) = plan([row(item, slot="a")], view).lists
    assert (listed.title, listed.tagging_target, listed.list_kind) == (
        "espresso Books",
        BOOKS,
        30394,
    )
    assert listed.d_tag.endswith("-espresso-dlcccccccc-books")


@pytest.mark.parametrize(
    "kind, name",
    [(1, "Notes"), (20, "Pictures"), (30023, "Articles"), (30402, "Listings"),
     (39998, "Lists"), (39999, "List items"), (9999, "List items")],
)
def test_content_title_kind_names(kind, name):
    """The server's own table, exactly. A client display label ("Photos",
    "Open lists") would fail this."""
    assert KIND_NAMES[kind] == name
    assert len(KIND_NAMES) == 7


def test_unnamed_kind_gets_no_list_and_is_reported():
    view = TargetView(by_id={"2" * 64: note("2" * 64, kind=7)})
    planned = plan([row("2" * 64)], view)
    assert planned.lists == []
    assert planned.unnamed_kinds == [7]


def test_unresolved_target_is_counted_and_joins_no_list():
    planned = plan([row("2" * 64), row("3" * 64)], TargetView(by_id={"3" * 64: note("3" * 64)}))
    assert planned.unresolved_targets == 1
    assert [m.pubkey for m in planned.lists[0].members] == ["3" * 64]


def test_a_9998_header_that_wasnt_found_leaves_its_items_unresolved():
    item = f"39999:{'e' * 64}:dune"
    view = TargetView(
        by_coordinate={item: TaggedEvent("1" * 64, 39999, "e" * 64, "dune", ("9" * 64,), ())}
    )
    planned = plan([row(item, slot="a")], view)
    assert planned.lists == []
    assert planned.unresolved_targets == 1


# --- S2: a DList with both item kinds gets a list of each --------------------


def test_dlist_with_both_item_kinds_gets_a_list_of_each():
    item = f"39999:{'e' * 64}:dune"
    view = TargetView(
        by_id={"7" * 64: TaggedEvent("7" * 64, 9999, "e" * 64, None, (BOOKS,), ())},
        by_coordinate={item: TaggedEvent("1" * 64, 39999, "e" * 64, "dune", (BOOKS,), ())},
        headers=HEADERS,
    )
    lists = plan([row(item, slot="a"), row("7" * 64)], view).lists
    assert sorted((x.list_kind, x.title, x.tagging_target) for x in lists) == [
        (30393, "espresso Books", BOOKS),
        (30394, "espresso Books", BOOKS),
    ]


def test_more_than_100_members_published_whole():
    ids = [f"{n:064x}" for n in range(150)]
    view = TargetView(by_id={i: note(i) for i in ids})
    (listed,) = plan([row(i) for i in ids], view).lists
    assert len(listed.members) == 150
    tags = build_content_list_tags(
        list_kind=30393,
        d_tag=listed.d_tag,
        observer=OBSERVER,
        title=listed.title,
        description="",
        tag_author_pubkey=TAG_AUTHOR,
        slug="espresso",
        tagging_target="1",
        members=listed.members,
        cutoff=1,
        min_rank=3,
    )
    assert len([t for t in tags if t[0] == "e"]) == 150
    assert len(json.loads(build_content_list_content(30393, listed.members))["members"]) == 150


# --- U17 (sentinel): people lists unchanged (AC17) ---------------------------


def test_people_lists_match_story_1_output():
    """Captured from the builder as it stood before story 2, for a fixed input."""
    golden = json.loads(
        (Path(__file__).parent / "fixtures" / "trusted_list_30392_golden.json").read_text()
    )
    taggings = [tuple(t) for t in golden["input"]["taggings"]]
    members = compute_members(
        taggings, cutoff=golden["input"]["cutoff"], rigor=golden["input"]["rigor"]
    )
    tags = build_trusted_list_tags(
        observer=OBSERVER,
        tag_event_id="c" * 64,
        tag_author_pubkey="d" * 64,
        slug="podcaster",
        name="Podcaster",
        description="Makes podcasts",
        members=members,
        cutoff=1,
        min_rank=3,
        rigor=0.65,
    )
    assert tags == golden["tags"]
    assert build_trusted_list_content(members) == golden["content"]
    retraction = build_trusted_list_tags(
        observer=OBSERVER,
        tag_event_id="",
        tag_author_pubkey="dddddddd",
        slug="chef",
        name="chef",
        description="",
        members=[],
        cutoff=1,
        min_rank=3,
        retracted=True,
    )
    assert retraction == golden["retraction_tags"]
