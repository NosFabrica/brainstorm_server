"""kind-30392 wire shape (AC9) and the d-tag identity (ADR D5)."""
from __future__ import annotations

import json

from app.services.trusted_list_build import (
    D_TAG_PREFIX,
    TRUSTED_LIST_METRIC,
    Member,
    build_trusted_list_content,
    build_trusted_list_tags,
    compute_d_tag,
)

OBS = "0" * 64
AUTH = "1" * 64
TAGEV = "2" * 64
M1 = "3" * 64
M2 = "4" * 64


def _tags(**over):
    kwargs = dict(
        observer=OBS,
        tag_event_id=TAGEV,
        tag_author_pubkey=AUTH,
        slug="podcaster",
        name="Podcaster",
        description="Makes podcasts",
        members=[Member(M1, 3, 0, 71), Member(M2, 1, 0, 50)],
        cutoff=1,
        min_rank=3,
    )
    kwargs.update(over)
    return build_trusted_list_tags(**kwargs)


def _find(tags, key):
    return [t for t in tags if t[0] == key]


def test_tl_event_wire_shape():
    tags = _tags()
    assert _find(tags, "d")[0][1] == f"{D_TAG_PREFIX}-{OBS[:8]}-{AUTH[:8]}-podcaster"
    assert _find(tags, "title")[0][1] == "Podcaster"
    assert _find(tags, "metric")[0][1] == TRUSTED_LIST_METRIC
    assert _find(tags, "observer")[0][1] == OBS
    assert _find(tags, "source-tag")[0] == ["source-tag", TAGEV, AUTH, "podcaster"]
    assert _find(tags, "cutoff")[0][1] == "1"
    assert _find(tags, "min-rank")[0][1] == "3"
    assert [t[1] for t in _find(tags, "p")] == [M1, M2]
    # D12 wire: ["p", <pubkey>, "", "<score>"] — empty relay slot, score third
    # and stringified, matching what tapestry's reader already parses.
    assert _find(tags, "p") == [["p", M1, "", "71"], ["p", M2, "", "50"]]
    assert _find(tags, "rigor")[0] == ["rigor", "0.5"]


def test_tl_carries_tag_description():
    # Issue #73 §4: title AND description ride the TL to aid searchability.
    # Tapestry's TLs carry no description tag; this is an addition.
    assert _find(_tags(), "description")[0][1] == "Makes podcasts"


def test_tl_metric_is_not_tapestrys_pinned_variant():
    # Sharing `tl-pin-`/`pinned-tag-membership` would collide with tapestry's
    # retraction sweep on any relay mirroring both derivations (ADR D5).
    tags = _tags()
    assert not _find(tags, "d")[0][1].startswith("tl-pin-")
    assert _find(tags, "metric")[0][1] != "pinned-tag-membership"


def test_d_tag_distinguishes_observers_and_tag_authors():
    other = "9" * 64
    assert compute_d_tag(OBS, AUTH, "x") != compute_d_tag(other, AUTH, "x")
    assert compute_d_tag(OBS, AUTH, "x") != compute_d_tag(OBS, other, "x")


def test_retraction_is_empty_membership_plus_marker():
    tags = _tags(retracted=True)
    assert _find(tags, "p") == []
    assert ["status", "retracted"] in tags
    # No membership means nothing to score: a retraction carries no rigor.
    assert _find(tags, "rigor") == []
    # The slot must stay identifiable after retraction.
    assert _find(tags, "d")[0][1] == compute_d_tag(OBS, AUTH, "podcaster")
    assert _find(tags, "observer")[0][1] == OBS


def test_content_carries_per_member_counts():
    payload = json.loads(build_trusted_list_content([Member(M1, 3, 1, 42)]))
    assert payload["members"] == [
        {"pubkey": M1, "endorsements": 3, "disputes": 1, "score": 42}
    ]


def test_content_for_empty_membership_is_valid_json():
    assert json.loads(build_trusted_list_content([])) == {"members": []}
