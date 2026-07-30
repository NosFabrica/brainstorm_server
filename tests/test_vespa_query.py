"""What the query builder actually emits.

`vespa_query` decides what search can match, and until now nothing tested it.
That is not an incidental gap: it is why `({defaultIndex:"name",prefix:true}
userInput(@w))` shipped for a month as a silent no-op — Vespa accepts the
annotation, drops it, and returns a plain exact match with no error anywhere. No
staging check catches a matcher that quietly does nothing; only an assertion on
the emitted YQL does.

These are pure string assertions (stdlib-only module, no services), so they run
in the fast suite and pin the two properties that failed silently:

  * prefix/fuzzy are DIRECT terms against the attribute fields, never userInput()
  * the budget that bounds them is actually applied

See docs/search-vs-tapestry.md §10/§11 and the note above `field name` in the
chart's vespa-app/schemas/doc.sd.
"""

import pytest

from app.core.vespa_query import (
    MAX_TYPO_EDITS,
    MIN_PREFIX_LEN,
    NAME_GRAM_RECALL,
    _word_max_edits,
    build_query,
)


def _yql(text: str) -> str:
    return build_query(text)[1]


# ---------------------------------------------------------------------------
# The regression this file exists for
# ---------------------------------------------------------------------------
def test_prefix_is_a_direct_term_not_userinput():
    """The original bug: a prefix annotation on userInput() is silently dropped.

    Asserting the *shape* is the point — a test that only checked "does a prefix
    clause exist" would have passed throughout the outage.
    """
    yql = _yql("odell")
    assert 'name_parts contains ({prefix:true}@w0)' in yql
    assert 'name_tokens contains ({prefix:true}@w0)' in yql
    # The form that parses, runs, and does nothing.
    assert "prefix:true}userInput" not in yql


def test_fuzzy_is_a_direct_term_not_userinput():
    yql = _yql("odelling")
    assert "name_parts contains ({maxEditDistance:1,prefixLength:2}fuzzy(@w0))" in yql
    assert "fuzzy:{maxEditDistance" not in yql


def test_prefix_and_fuzzy_never_target_index_only_fields():
    """about/website/nip05/lud16 have no attribute sibling. A prefix or fuzzy
    term against them is an ERROR (HTTP 400), not a no-op."""
    yql = _yql("something")
    for field in ("about", "website", "nip05", "lud16", "name", "display_name"):
        assert f"{field} contains ({{prefix:true}}" not in yql
        assert f"{field} contains ({{maxEditDistance" not in yql


# ---------------------------------------------------------------------------
# Bounds: every match must be within a stated distance of the query
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    "word,expected",
    [("ab", 0), ("ode", 0), ("odel", 1), ("odelling", 1), ("odellington", 2),
     ("odellingtonshire", 3)],
)
def test_typo_budget_ladder(word, expected):
    assert _word_max_edits(word) == expected


def test_typo_budget_never_exceeds_the_ceiling():
    long_word = "a" * 60
    assert _word_max_edits(long_word) <= MAX_TYPO_EDITS


def test_only_the_top_edit_distance_is_emitted():
    """maxEditDistance:N subsumes every smaller N — emitting a clause per tier is
    duplicate matching work."""
    yql = _yql("odellington")  # 2-edit budget
    assert "maxEditDistance:2" in yql
    assert "maxEditDistance:1" not in yql


def test_short_words_get_no_fuzzy_clause():
    yql = _yql("ode")
    assert "fuzzy(" not in yql


def test_prefix_floor_applies_to_latin():
    assert "prefix:true" not in _yql("od")
    assert "prefix:true" in _yql("ode")
    assert MIN_PREFIX_LEN == 3


def test_prefix_floor_is_lower_for_non_ascii():
    """A 2-character CJK query is as specific as a 5-6 character Latin one; the
    Latin floor made such names unreachable."""
    assert "prefix:true" in _yql("中村")


def test_name_trigram_recall_is_off():
    """The one matcher with no bound on how different a hit could be: a trigram
    OR matched anything sharing 3 characters, so "ode" pulled in "model"."""
    assert NAME_GRAM_RECALL is False
    yql = _yql("ode")
    assert "name_gram contains" not in yql
    assert "display_name_gram contains" not in yql


def test_about_trigrams_need_enough_grams_to_discriminate():
    """about_gram is the bio-side equivalent. At one or two trigrams the AND
    degenerates into a bare substring test — "ode" reached a bio reading
    "hosted by ODELL"."""
    assert "about_gram contains" not in _yql("ode")
    assert "about_gram contains" not in _yql("odel")
    assert "about_gram contains" in _yql("odell")


# ---------------------------------------------------------------------------
# Shape that the rest of the pipeline depends on
# ---------------------------------------------------------------------------
def test_exact_clause_still_targets_the_index_fields():
    """matchCount(name)/matchCount(display_name) drive doc.sd's exact tier, so
    the exact clause must keep using userInput() against the INDEX fields."""
    yql = _yql("odell")
    assert '{defaultIndex:"name",label:"mtch_exact"}userInput(@w0)' in yql
    assert '{defaultIndex:"display_name",label:"mtch_exact"}userInput(@w0)' in yql


def test_params_bind_every_referenced_word():
    words, yql, params = build_query("vitor pamplona")
    for var in params:
        assert f"@{var}" in yql
    assert params["w0"] == "vitor"
    assert params["w1"] == "pamplona"
    # >=2 words also emit the joined variant
    assert params["wj"] == "vitorpamplona"


def test_word_cap_is_enforced():
    words, yql, params = build_query(" ".join(f"w{i}" for i in range(20)))
    assert len(words) <= 6
