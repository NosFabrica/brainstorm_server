"""Pure, dependency-free YQL query builders for the Vespa profile search.

Extracted from `vespa.py` so the EXACT same query the live search sends can be
reused by diagnostic tooling (`scripts/analyze_pubkey.py`) without importing
settings/httpx. Stdlib-only — no third-party imports — so a standalone script can
build the query and run it against a port-forwarded Vespa.

See docs/search-vs-tapestry.md §8.4 / §10 / §11.
"""

# How many query words we label / parametrize at most.
MAX_QUERY_WORDS = 6

# Field roles for match labeling (consumed by match_quality / affiliation_match
# in doc.sd). NB itemRawScore does NOT populate for plain text terms, so the
# labels are currently inert (§11.1) — kept for a future verbatim-field revival.
# The real name-tier gate is matchCount() in doc.sd's has_token_match(), which
# must list the same primary fields.
#   primary     — name/display_name/nip05/lud16 (nip05 & lud16 are both
#                 @-address identity fields, so lud16 is treated like nip05)
#   affiliation — bio + website (only the EXACT clause labeled)
# (The deprecated `username` folds into `name` at ingest per NIP-24, so it is no
# longer a stored/searchable field — see process_strfry_event._FIELD_RESOLUTION.)
_PRIMARY_FIELDS = ("name", "display_name", "nip05", "lud16")
_AFFILIATION_FIELDS = ("about", "website")
_SEARCH_FIELDS = ("name", "display_name", "about", "nip05", "lud16", "website")


def _field_role(field: str) -> str:
    if field in _PRIMARY_FIELDS:
        return "primary"
    if field in _AFFILIATION_FIELDS:
        return "affiliation"
    return "recall"


def _gram_clause(text: str, gram_field: str, gram_size: int = 3) -> str:
    """OR of every trigram in `text` against `gram_field`."""
    grams = set()
    for word in text.lower().split():
        for i in range(max(1, len(word) - gram_size + 1)):
            g = word[i : i + gram_size]
            if len(g) == gram_size and g.isalnum():
                grams.add(g)
    if not grams:
        return ""
    return (
        "(" + " or ".join(f'{gram_field} contains "{g}"' for g in sorted(grams)) + ")"
    )


def _about_gram_clause_for_word(word: str, gram_size: int = 3) -> str:
    """AND of one word's trigrams against `about_gram` (discriminative)."""
    grams = [
        word[i : i + gram_size]
        for i in range(len(word) - gram_size + 1)
        if word[i : i + gram_size].isalnum()
        and len(word[i : i + gram_size]) == gram_size
    ]
    if not grams:
        return ""
    return "(" + " and ".join(f'about_gram contains "{g}"' for g in grams) + ")"


def _word_max_edits(word: str) -> int:
    """Per-word fuzzy budget, length-gated like Meilisearch: <4 → exact/prefix
    only, ≥4 → 1 edit, ≥9 → 2 edits. See docs §10."""
    if len(word) >= 9:
        return 2
    if len(word) >= 4:
        return 1
    return 0


def _field_clauses(field: str, var: str, max_edits: int, role: str) -> list[str]:
    """Match clauses for one (field, word): exact, prefix, and up to two fuzzy
    tiers. Labels depend on the field `role` (§10 / §11)."""

    def _ann(extra: list[str], label: str | None) -> str:
        ann = [f'defaultIndex:"{field}"'] + extra
        if label:
            ann.append(f'label:"{label}"')
        return "{" + ",".join(ann) + "}"

    ex_label = {"primary": "mtch_exact", "affiliation": "mtch_affil"}.get(role)
    pf_label = "mtch_prefix" if role == "primary" else None
    clauses = [
        f"({_ann([], ex_label)}userInput({var}))",
        f"({_ann(['prefix:true'], pf_label)}userInput({var}))",
    ]
    # prefixLength:2 — the first two characters must match exactly.
    if max_edits >= 1:
        fz1 = "mtch_fz1" if role == "primary" else None
        clauses.append(
            f"({_ann(['fuzzy:{maxEditDistance:1,prefixLength:2}'], fz1)}userInput({var}))"
        )
    if max_edits >= 2:
        fz2 = "mtch_fz2" if role == "primary" else None
        clauses.append(
            f"({_ann(['fuzzy:{maxEditDistance:2,prefixLength:2}'], fz2)}userInput({var}))"
        )
    return clauses


def _word_group(var: str, literal: str, with_grams: bool = True) -> str:
    """All match clauses for one query word across the searchable fields + grams."""
    me = _word_max_edits(literal)
    clauses: list[str] = []
    for field in _SEARCH_FIELDS:
        clauses += _field_clauses(field, var, me, _field_role(field))
    if with_grams:
        for gram_field in ("name_gram", "display_name_gram"):
            gc = _gram_clause(literal, gram_field)
            if gc:
                clauses.append(gc)
        agc = _about_gram_clause_for_word(literal)
        if agc:
            clauses.append(agc)
    return "(" + " or ".join(clauses) + ")"


def _build_yql(words: list[str], joined, pairs: list[str]) -> str:
    """Per-word groups OR'd together, plus a joined-CamelCase variant and
    adjacent-pair concatenations for >2-word queries."""
    parts = [_word_group(f"@w{i}", w) for i, w in enumerate(words[:MAX_QUERY_WORDS])]
    if joined:
        parts.append(_word_group("@wj", joined, with_grams=False))
    for i, _ in enumerate(pairs):
        parts.append(_word_group(f"@wp{i}", pairs[i], with_grams=False))
    return f"select * from doc where {' or '.join(parts)}"


def build_query(query_text: str):
    """Return (words, yql, word_params) — the EXACT YQL + per-word params the
    live search sends (minus ranking features). Reused by search() and by
    diagnostic tooling so they never drift."""
    words = query_text.split()[:MAX_QUERY_WORDS]
    joined = "".join(words) if len(words) >= 2 else None
    pairs = (
        ["".join(words[i : i + 2]) for i in range(len(words) - 1)]
        if len(words) >= 3
        else []
    )
    yql = _build_yql(words, joined, pairs)
    params = {f"w{i}": w for i, w in enumerate(words)}
    if joined:
        params["wj"] = joined
    for i, p in enumerate(pairs):
        params[f"wp{i}"] = p
    return words, yql, params
