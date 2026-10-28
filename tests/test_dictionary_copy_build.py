"""The Assistant's concept copies as pure functions: the tags, the outcome
given what the Assistant already holds, and the date (ADR
assistant-dictionary/0001 D2, D4, D5).

The copy's tags are a cross-repo constant. Brainstorm-UI's `exactCopyTemplate`
and `withdrawnTemplate` (`client/src/lib/conceptCopy.ts`) make the same ones,
so the fixtures below are that repo's `lib/conceptCopy.test.ts` fixtures,
verbatim (AC11). The server adds only its client tag, last.
"""

from __future__ import annotations

import pytest

from app.services.dictionary_copy_build import (
    add_outcome,
    copy_tags,
    next_created_at,
    parse_concept_coordinate,
    points_at,
    withdraw_outcome,
    withdrawn_tags,
)

AVI = "b83a28b7e4e5d20bd960c5faeb6625f95529166b8bdb045d42634a2f35919450"
ASSISTANT = "2" * 64
COMMUNITY = f"39998:{AVI}:github-accounts"
OTHER = f"39998:{'3' * 64}:github-accounts"
CLIENT = ["client", "Brainstorm"]

# Brainstorm-UI `lib/conceptCopy.test.ts`, describe("exactCopyTemplate"): the
# community header, with tags a copy must leave behind (json, concept-graph,
# license, alt, client, and its own self-pointing b)...
UI_COMMUNITY_TAGS = [
    ["d", "github-accounts"],
    ["names", "GitHub Account", "GitHub Accounts"],
    ["json", "{}"],
    ["description", "A list of github handles/accounts"],
    ["concept-graph", "x"],
    ["required", "github-username"],
    ["field-type", "github-username", "text"],
    ["optional", "description", "Who it belongs to"],
    ["display", "title", "github-username"],
    ["image", "https://example.com/gh.png"],
    ["link", "f" * 64, "wss://dcosl.brainstorm.world", "username", "github-username"],
    ["license", "CC0"],
    ["alt", "GitHub accounts"],
    ["client", "Tapestry"],
    ["b", COMMUNITY, "pointer"],
]
# ...and the tags that test expects of the copy.
UI_EXPECTED_COPY = [
    ["d", "github-accounts"],
    ["names", "GitHub Account", "GitHub Accounts"],
    ["description", "A list of github handles/accounts"],
    ["required", "github-username"],
    ["field-type", "github-username", "text"],
    ["optional", "description", "Who it belongs to"],
    ["display", "title", "github-username"],
    ["image", "https://example.com/gh.png"],
    ["link", "f" * 64, "wss://dcosl.brainstorm.world", "username", "github-username"],
    ["b", COMMUNITY, "pointer"],
]
# Brainstorm-UI `lib/conceptCopy.ts` `withdrawnTemplate`, for the same `d`.
UI_WITHDRAWN = [
    ["d", "github-accounts"],
    ["b", "b-tag-deferred"],
    [
        "alt",
        "A withdrawn concept copy: considered, and affiliated with no community concept.",
    ],
]


def header(pubkey: str, tags: list[list[str]], created_at: int = 10) -> dict:
    return {
        "id": "0" * 64,
        "pubkey": pubkey,
        "kind": 39998,
        "created_at": created_at,
        "content": "",
        "tags": tags,
        "sig": "0" * 128,
    }


COMPOSED = copy_tags(header(AVI, UI_COMMUNITY_TAGS), COMMUNITY)


def held(tags: list[list[str]], created_at: int = 10) -> dict:
    """The Assistant's newest header at the concept's `d`."""
    return header(ASSISTANT, tags, created_at)


# B1 (AC1, AC11)
def test_the_copy_is_the_ui_fixture_verbatim_then_the_client_tag():
    assert COMPOSED[:-1] == UI_EXPECTED_COPY
    assert COMPOSED[-1] == CLIENT


# B2 (AC1)
def test_the_copy_keeps_the_headers_own_order_and_every_repeat():
    coordinate = f"39998:{AVI}:x"
    community = header(
        AVI,
        [
            ["d", "x"],
            ["optional", "b"],
            ["names", "X", "Xs"],
            ["z", "list"],
            ["optional", "a"],
            ["name", "X"],
            ["slug", "x"],
            ["e", "1" * 64],
            ["allowed", "c"],
            ["recommended", "e"],
        ],
    )
    assert copy_tags(community, coordinate) == [
        ["d", "x"],
        ["optional", "b"],
        ["names", "X", "Xs"],
        ["optional", "a"],
        ["name", "X"],
        ["slug", "x"],
        ["allowed", "c"],
        ["recommended", "e"],
        ["b", coordinate, "pointer"],
        CLIENT,
    ]


# B3 (AC5, AC11)
def test_the_withdrawn_form_is_the_uis_then_the_client_tag():
    tags = withdrawn_tags("github-accounts")
    assert tags[:-1] == UI_WITHDRAWN
    assert tags[-1] == CLIENT


# B4 (AC8)
@pytest.mark.parametrize(
    "value, expected",
    [
        (COMMUNITY, (AVI, "github-accounts")),
        (f"39998:{AVI}:lists:github:accounts", (AVI, "lists:github:accounts")),
    ],
)
def test_a_concept_coordinate_splits_at_its_first_two_colons(value, expected):
    assert parse_concept_coordinate(value) == expected


# B5 (AC8)
@pytest.mark.parametrize(
    "value",
    [
        "",
        "39998",
        f"39998:{AVI}",
        f"39998:{AVI}:",
        f"39999:{AVI}:x",
        f"30382:{AVI}:x",
        "39998:abc:x",
        f"39998:{AVI.upper()}:x",
        f"39998:{AVI}0:x",
        f" 39998:{AVI}:x",
    ],
)
def test_anything_else_is_not_a_concept_coordinate(value):
    assert parse_concept_coordinate(value) is None


# B6 (D4)
@pytest.mark.parametrize(
    "b",
    [
        ["b", COMMUNITY, "pointer"],
        ["b", COMMUNITY],
        ["b", COMMUNITY, "inherit"],
        ["b", COMMUNITY, "inherit-items"],
        ["b", COMMUNITY, "a-type-nobody-defined-yet"],
    ],
)
def test_any_b_naming_the_coordinate_points_at_it(b):
    assert points_at(held([["d", "github-accounts"], b]), COMMUNITY)


# B7 (D4)
@pytest.mark.parametrize(
    "tags",
    [
        [["d", "github-accounts"], ["b", "b-tag-deferred"]],
        [["d", "github-accounts"], ["b", OTHER, "pointer"]],
        [["d", "github-accounts"]],
    ],
)
def test_a_deferred_b_another_target_or_no_b_doesnt(tags):
    assert not points_at(held(tags), COMMUNITY)


# B8 (AC1)
def test_with_nothing_held_adding_publishes():
    assert add_outcome(COMPOSED, None, COMMUNITY) == "published"


# B9 (AC6)
def test_a_withdrawn_copy_is_replaced_by_a_fresh_one():
    assert (
        add_outcome(COMPOSED, held(withdrawn_tags("github-accounts")), COMMUNITY)
        == "published"
    )


# B10 (AC2)
def test_the_same_copy_is_unchanged():
    assert add_outcome(COMPOSED, held(COMPOSED), COMMUNITY) == "unchanged"


# B11 (AC2)
def test_the_client_tag_doesnt_count_as_a_difference():
    without_client = [t for t in COMPOSED if t[0] != "client"]
    assert add_outcome(COMPOSED, held(without_client), COMMUNITY) == "unchanged"


# B12 (AC3)
def test_a_copy_of_an_older_definition_is_updated():
    older = [
        ["description", "Older words"] if t[0] == "description" else t for t in COMPOSED
    ]
    assert add_outcome(COMPOSED, held(older), COMMUNITY) == "updated"


# B13 (D4: Tapestry's upgrade path)
def test_a_b_of_another_type_to_the_same_target_is_updated_to_a_pointer():
    inherit = [
        ["b", COMMUNITY, "inherit-items"] if t[0] == "b" else t for t in COMPOSED
    ]
    assert add_outcome(COMPOSED, held(inherit), COMMUNITY) == "updated"


# B14 (AC4)
@pytest.mark.parametrize(
    "tags",
    [
        [["d", "github-accounts"], ["b", OTHER, "pointer"]],
        [["d", "github-accounts"], ["names", "My own list", "My own lists"]],
        [["d", "github-accounts"], ["b", "b-tag-deferred"], ["b", OTHER, "pointer"]],
    ],
)
def test_any_other_header_at_that_d_is_a_conflict(tags):
    assert add_outcome(COMPOSED, held(tags), COMMUNITY) == "conflict"


# B15 (AC5)
def test_a_copy_that_points_here_is_withdrawn():
    assert withdraw_outcome(held(COMPOSED), COMMUNITY) == "withdrawn"


# B16 (AC5)
@pytest.mark.parametrize("newest", [None, held(withdrawn_tags("github-accounts"))])
def test_with_nothing_to_withdraw_withdrawing_is_unchanged(newest):
    assert withdraw_outcome(newest, COMMUNITY) == "unchanged"


# B17 (AC5)
@pytest.mark.parametrize(
    "tags",
    [
        [["d", "github-accounts"], ["b", OTHER, "pointer"]],
        [["d", "github-accounts"], ["names", "My own list", "My own lists"]],
    ],
)
def test_withdrawing_never_touches_a_header_that_isnt_this_copy(tags):
    assert withdraw_outcome(held(tags), COMMUNITY) == "conflict"


# B18 (AC10)
@pytest.mark.parametrize(
    "now, newest_at, expected",
    [
        (100, None, 100),
        (100, 50, 100),
        (100, 100, 101),
        (100, 500, 501),
    ],
)
def test_a_new_version_is_dated_after_the_newest(now, newest_at, expected):
    newest = None if newest_at is None else held(COMPOSED, created_at=newest_at)
    assert next_created_at(now, newest) == expected
