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

# Attribute fields in doc.sd that prefix/fuzzy terms are matched against. Both
# are built from name + display_name, at two different granularities, and BOTH
# are needed:
#
#   name_parts   split at every word START, including inside a compound name
#                (camelCase + any non-alphanumeric run). This is what makes
#                "meme" find "BitcoinMemeTreasury" and "lover" find
#                "CoffeeLover" — splitting on spaces alone found neither.
#   name_tokens  whole space-delimited tokens. Required because name_parts alone
#                REGRESSES the compound-prefix case: "VitorPamplona" splits to
#                [vitor, pamplona] and "vitorp" prefixes neither.
#
# They are OR'd, so a word matching either granularity is a near hit.
_NEAR_FIELDS = ("name_parts", "name_tokens")

# Both matchers need TWO things that were missing until 2026-07-30 (verified
# against a scratch Vespa; see the note above `field name` in doc.sd):
#
#   1. a DIRECT term. `userInput()` silently drops `prefix`/`fuzzy` annotations
#      from the terms it builds, so `({defaultIndex:"name",prefix:true}
#      userInput(@w))` ran for months as a plain exact match, with no error.
#   2. an ATTRIBUTE field. Against an `index` field the direct form is rejected
#      outright ("'name' is not an attribute field: Prefix matching is not
#      supported"); fuzzy() returns HTTP 400.
#
# Cause 1 masked cause 2 by turning it into a no-op, which is why "Ode" and
# "Odel" could never find "ODELL" and nothing ever surfaced an error.
#
# Fields without an attribute sibling (about/website/nip05/lud16) stay exact-only:
# a prefix or fuzzy term against them is an ERROR, not a no-op.

# Minimum word length for a prefix clause. Prefix is a posting-list scan over
# every term sharing the prefix, so 1-2 Latin characters would sweep a large
# slice of a multi-million-doc corpus for no precision gain. 3 also matches the
# trigram floor, so nothing that previously worked stops working.
MIN_PREFIX_LEN = 3

# ...but 3 is a LATIN heuristic. A 2-character CJK query ("中村") is as specific
# as a 5-6 character Latin one, and a flat 3 made such names unfindable — the
# whole point of the unicode-aware split is undone if the query can't reach them.
# Any word carrying a non-ASCII character gets the lower floor; those dictionaries
# are sparse enough that a 2-character prefix stays cheap.
MIN_PREFIX_LEN_NON_ASCII = 2


def _min_prefix_len(word: str) -> int:
    return MIN_PREFIX_LEN if word.isascii() else MIN_PREFIX_LEN_NON_ASCII

# Trigram recall on the NAME fields — OFF since 2026-07-30.
#
# `_gram_clause` ORs every trigram of a word against name_gram/display_name_gram,
# so ANY doc sharing a single 3-character sequence matched. It was the one
# matcher with NO bound on how different a hit could be, and no typo budget can
# constrain it. Measured on a scratch Vespa:
#     grams "ode"  -> model, code, ODELL, Odessa, Ode
#     grams "odel" -> model, code, CITADELDISPATCH, ODELL, Odessa, Ode
# "model" and "CITADELDISPATCH" are not within ANY edit budget of the query.
#
# It existed to paper over prefix/fuzzy being broken. Now that both work, real
# prefix + a length-gated typo budget cover the same recall with a defined bound.
#
# TRADE-OFF: this drops INFIX matching. Prefix is anchored at the start and fuzzy
# keeps prefixLength:2, so "dell" no longer finds "ODELL". That is the price of
# every hit being within a stated edit distance. Flip this back to True to trade
# the bound for that recall — the name_gram fields stay in doc.sd either way, and
# primary_text()'s bm25(name_gram) term simply reads 0 while this is off.
#
# about_gram is deliberately NOT affected: bio search has no prefix/fuzzy path,
# and its clause ANDs every trigram of the word (far tighter than this OR).
NAME_GRAM_RECALL = False


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


# Minimum trigrams before the about_gram clause is worth emitting. A word of
# length L yields L-2 trigrams, so this is "word >= 5 chars".
#
# about_gram is the LAST unbounded matcher: bios have no prefix/fuzzy path, so
# this AND-of-trigrams is the only partial-word route into `about`. At one or two
# trigrams it degenerates into a bare substring test with no bound at all —
# measured, `about_gram contains "ode"` matched "model", "code" and a bio reading
# "hosted by ODELL", none within any typo budget of "ode". At 3+ AND'ed trigrams
# it is selective enough to be worth its recall.
#
# Short queries still reach bios through the exact-token clause on `about`.
_MIN_ABOUT_GRAMS = 3


def _about_gram_clause_for_word(word: str, gram_size: int = 3) -> str:
    """AND of one word's trigrams against `about_gram`, when there are enough of
    them to be discriminative (see _MIN_ABOUT_GRAMS)."""
    grams = [
        word[i : i + gram_size]
        for i in range(len(word) - gram_size + 1)
        if word[i : i + gram_size].isalnum()
        and len(word[i : i + gram_size]) == gram_size
    ]
    if len(grams) < _MIN_ABOUT_GRAMS:
        return ""
    return "(" + " and ".join(f'about_gram contains "{g}"' for g in grams) + ")"


# Hard ceiling on typos, per the team's call (2026-07-30): a hit needing more
# than this many edits is not matched at all. Vespa's maxEditDistance enforces it
# at match time, so over-budget docs never enter the candidate set — they are not
# merely ranked low.
MAX_TYPO_EDITS = 3


def _word_max_edits(word: str) -> int:
    """Per-word typo budget, length-gated. Capped at MAX_TYPO_EDITS.

    The gating is the important part: an edit budget is only meaningful as a
    FRACTION of the word. Measured on a scratch Vespa, 3 edits against the
    6-letter "odelll" matches "Odessa" — not a typo, a different word. Every tier
    below holds the ratio near ~22-25%, the same place Meilisearch sits:

        <4     0 edits   any edit on a 3-letter word is a different word
        4-8    1         25% .. 12%
        9-12   2         22% .. 17%
        >=13   3         23% .. less     <- the ceiling, long handles only

    Note this budget does NOT govern prefix matching, which is a different
    relation: "vitorp" -> "VitorPamplona" appends 7 characters but is not 7
    typos, it is an unfinished word. Prefix is bounded by _min_prefix_len and by
    being anchored at the start; see _field_clauses.
    """
    n = len(word)
    if n >= 13:
        return min(3, MAX_TYPO_EDITS)
    if n >= 9:
        return min(2, MAX_TYPO_EDITS)
    if n >= 4:
        return min(1, MAX_TYPO_EDITS)
    return 0


def _field_clauses(field: str, var: str, role: str) -> list[str]:
    """The exact clause for one (field, word), against the INDEX field via
    userInput(). This is what feeds matchCount(name)/matchCount(display_name),
    i.e. doc.sd's exact tier.

    Prefix/fuzzy are NOT emitted here — they are per-word, not per-field, and go
    to the merged attribute fields via `_near_clauses`.
    """
    ann = [f'defaultIndex:"{field}"']
    label = {"primary": "mtch_exact", "affiliation": "mtch_affil"}.get(role)
    if label:
        ann.append(f'label:"{label}"')
    return ["({" + ",".join(ann) + "}" + f"userInput({var}))"]


def _near_clauses(var: str, literal: str, allow_fuzzy: bool = True) -> list[str]:
    """Prefix + fuzzy clauses for one word, against the merged attribute fields.

    Emitted ONCE per word (not per source field) because name_parts/name_tokens
    already merge name + display_name. These feed matchCount(name_parts) /
    matchCount(name_tokens), i.e. doc.sd's near tier — which is how an exact hit
    still ranks above a prefix or typo hit.

    Direct terms, never userInput(): userInput() silently drops prefix/fuzzy
    annotations (see _NEAR_FIELDS).

    `allow_fuzzy=False` for the synthetic joined/pair variants — see _word_group.
    """
    out: list[str] = []
    if len(literal) >= _min_prefix_len(literal):
        for f in _NEAR_FIELDS:
            out.append(f"({f} contains ({{prefix:true}}{var}))")
    # prefixLength:2 — the first two characters must match exactly, which also
    # bounds how much of the attribute dictionary the fuzzy matcher walks.
    # Only the top budget is emitted: maxEditDistance:N subsumes every smaller N,
    # so the old per-tier clauses were pure duplicate work once the match_quality
    # labels they fed turned out to be inert (§11.1).
    edits = _word_max_edits(literal) if allow_fuzzy else 0
    if edits:
        for f in _NEAR_FIELDS:
            out.append(
                f"({f} contains ({{maxEditDistance:{edits},prefixLength:2}}fuzzy({var})))"
            )
    return out


def _word_group(var: str, literal: str, synthetic: bool = False) -> str:
    """All match clauses for one query word.

    `synthetic=True` marks the joined / adjacent-pair CONCATENATIONS built in
    build_query, not words the user typed. They get exact + prefix but NO fuzzy
    and no trigrams:

      * fuzzy — "satoshi nakamoto bitcoin" builds "satoshinakamotobitcoin", 22
        characters, which draws the TOP typo budget (3 edits). That is the single
        most expensive matcher we have, walking the attribute dictionary for a
        token nobody typed, and a 3-word query emitted six of them (joined + 2
        pairs, x2 fields). Prefix on the concatenation is cheap and does the
        useful work ("vitorp" -> Vitor Pamplona), so it stays.
      * trigrams — the concatenation's trigrams are a superset of the words'
        own, so they add recall noise without adding reach.
    """
    clauses: list[str] = []
    for field in _SEARCH_FIELDS:
        clauses += _field_clauses(field, var, _field_role(field))
    clauses += _near_clauses(var, literal, allow_fuzzy=not synthetic)
    if not synthetic:
        if NAME_GRAM_RECALL:
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
        parts.append(_word_group("@wj", joined, synthetic=True))
    for i, _ in enumerate(pairs):
        parts.append(_word_group(f"@wp{i}", pairs[i], synthetic=True))
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
