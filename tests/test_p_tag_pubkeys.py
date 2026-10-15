"""`p` tags become graph edges, so their pubkeys must be canonical hex."""

from app.services.report_graph_service import extract_report_targets
from app.utils.nostr import p_tag_pubkeys

A = "a" * 64
B = "b" * 64


def test_uppercase_is_lowercased_and_deduplicated():
    tags = [["p", A.upper()], ["p", A], ["p", B, "wss://relay"]]
    assert p_tag_pubkeys(tags) == [A, B]


def test_non_hex_and_malformed_tags_are_dropped():
    tags = [["p", "npub1xyz"], ["p", "a" * 63], ["p"], [], ["e", A], ["p", None]]
    assert p_tag_pubkeys(tags) == []


def test_user_reports_use_the_same_normalisation():
    assert extract_report_targets({"tags": [["p", A.upper(), "spam"]]}) == [A]
