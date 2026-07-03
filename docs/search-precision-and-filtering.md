# Search precision + NIP-50 filtering: problems & fixes

_Last updated: 2026-06-27. Owner: search. Audience: the team — read this before
touching `app/core/vespa.py`, the NIP-50 relay, or the Vespa schema._

This note covers two related changes that landed together because they touch the
same files (`app/core/vespa.py` and the Vespa `doc.sd` schema):

1. **Precision fix** — exact-match search was being polluted by typo-correction
   and trigram recall. See "Problem 1".
2. **Sort/filter push-down** — the NIP-50 `sort:`/`filter:` extensions were
   prototyped as Python post-processing over an over-fetched candidate set. We
   moved that work into Vespa rank profiles. See "Problem 2".

> **Where the schema lives.** The *deployed* Vespa schema is
> `nosfabrica-kube/charts/brainstorm/vespa-app/schemas/doc.sd`, shipped via a
> Helm ConfigMap + a post-install/upgrade Job that POSTs the app package to the
> Vespa config server (see `charts/brainstorm/templates/vespa-app-deploy.yaml`
> and `charts/brainstorm/VESPA.md`). The copy under
> `brainstorm_one_click_deployment/vespa-app/` is **stale** (it predates the
> NaN-guard fix) — treat the kube repo as source of truth.

---

## Problem 1 — garbage in results; exact matching suffers

### Symptom

Searches return loosely-related junk, and exact-name queries don't reliably put
the exact match on top. It looked like "typo correction gone wrong" — and that's
half the story.

### Root causes

The query builder in `app/core/vespa.py` OR-s several recall-wideners into the
Vespa `where` clause. Three of them are responsible, and two also feed the
ranking score:

1. **Fuzzy matching was too loose.** `_word_max_edits()` allowed
   `maxEditDistance: 2` for words ≥ 6 chars, with `prefixLength: 1`. A single
   leading character plus "within two edits" pulls in a large cloud of unrelated
   names (an 8-char query can match hundreds of docs).

2. **`matchCount()` does not distinguish exact from fuzzy.** The ranking's
   dominant term is `matchCount(name) * 100`, but Vespa counts a *fuzzy* or
   *prefix* match toward `matchCount` exactly like an exact one. So a document
   matched two edits away earns the **same `+100`** as a genuine exact hit —
   which is why "exact matching suffers": after that term, only `bm25` and the
   trigram score break the tie, and those can favor the wrong doc.

3. **Trigram OR is a firehose.** `_gram_clause()` builds `(g1 or g2 or …)` over
   a word's trigrams against `name_gram`/`display_name_gram`. **Any single
   shared 3-character sequence** qualifies a doc into the candidate set
   ("robert" shares `rob/obe/ber/ert` with many unrelated names).

4. **The trigram weight let recall-helpers outrank exact matches.** In
   `search()`, `query(w_gram)` was **20.0** for short queries / 5.0 otherwise,
   and `name_text` adds `query(w_gram) * bm25(name_gram)`. At `w_gram = 20`, a
   trigram-only match (`matchCount = 0`) could score higher than a real exact
   token match. Trigrams are meant to be a recall safety-net and tie-breaker —
   not a primary ranking signal.

### Fix (the "Option A" tuning)

Conservative knob changes plus one structural guarantee. All low-risk; no
re-feed required (see "Deploying schema changes").

**In `app/core/vespa.py`:**

- `_word_max_edits()` — cap the fuzzy budget at **1 edit**, and only for words
  **≥ 4 chars**. One-edit typos are still corrected; the "within-2-of-anything"
  cloud is gone.
- `_field_clauses()` — fuzzy `prefixLength: 1 → 2`. The first two characters must
  match exactly, so a typo in char ≥ 3 is still tolerated but a wrong first/second
  letter no longer drags in noise.
- `search()` — lower `query(w_gram)` defaults (short-query / long-query). Trigrams
  remain meaningful for very short queries where token matching can't fire, but
  stop dominating longer queries.

**In `doc.sd` (`name_and_quality_score_only`):**

- Add a `query(gram_cap)` input and cap the trigram contribution:
  `+ min(query(w_gram) * bm25(name_gram), query(gram_cap))` in `name_text` and
  the equivalent in `display_name_text`. With `gram_cap` < 100, a trigram-only
  match can **never** outscore a single exact token match (`matchCount * 100`).
  This makes "exact beats trigram" a guarantee, not a tuning accident.

### Known limitation / future "Option B"

Cause #2 above is only *mitigated*, not eliminated: with fuzzy tightened to
1 edit + `prefixLength: 2`, a fuzzy hit is genuinely close, so it sharing the
`+100` with an exact hit is acceptable. If we later need exact to **strictly**
beat any fuzzy hit, we have to separate the two at match time — e.g. keep the
loose clause for recall but score a dedicated non-fuzzy clause separately (Vespa
`rank()` operator), or add an explicit full-exact-match bonus. That's a larger
change to `_build_yql` + the rank profile and is deferred.

### How to tune safely

These knobs interact. Do **not** eyeball them — tune against a fixed set of
`query → expected pubkey` pairs (the real garbage cases) run against a live
Vespa. Add cases as new garbage is reported.

---

## Problem 2 — NIP-50 `sort:` / `filter:` were going to run in Python

### Why Python post-processing was the wrong home

The per-observer `rank` metric is **not a stored field**. In `doc.sd` it is
computed at query time:

```
function user_score() {
    expression: sum(query(user_q) * attribute(quality_scores))
}
```

— a dot-product over the sparse `quality_scores` tensor, selecting the cell for
the observer passed in `query(user_q)`. Vespa can *rank* by that expression, but
a YQL `where` clause cannot *filter* on it, and there is no single attribute to
`order by`. The prototype therefore over-fetched (`EXTENSION_OVERFETCH = 200`)
and re-sorted/filtered in Python (`_apply_filters_and_sort`), because the
top-N by text relevance is not the same set as the top-N by rank, nor the subset
passing `rank ≥ N`.

That works but is wasteful and fragile (over-fetch tuning, double work, the app
re-implementing ranking). We pushed it into Vespa instead.

### The fix — dedicated rank profiles

`doc.sd` gains three profiles that inherit `name_and_quality_score_only` (so
`user_score()`, `relevance()`, the inputs, and match-features carry over):

| Profile | first-phase ordering | Used for |
|---|---|---|
| `rank_filtered` | default `relevance()` | `filter:` with no `sort:` |
| `rank_desc` | `user_score()` | `sort:rank:desc` (± filter) |
| `rank_asc` | `-1 * user_score()` (Vespa always orders desc) | `sort:rank:asc` (± filter) |

**Sorting** is just "make the metric the first-phase expression," so Vespa's
top-N *is* the answer — the over-fetch disappears and the server asks for exactly
`hits`.

**Filtering** uses a per-query threshold. Each profile gates on a shared
`query(min_rank)` input:

```
first-phase {
    expression: if(user_score() >= query(min_rank), <ordering>, -1e9)
    rank-score-drop-limit: -1e6
}
```

A hit failing the threshold is mapped to a sentinel (`-1e9`) far below
`rank-score-drop-limit` (`-1e6`), so Vespa drops it server-side. Real scores live
in `[-100, 1000]`, so the wide sentinel gap means the exact `<` vs `<=` drop
semantics never affect a real hit. `query(min_rank)` defaults to a value below
every real score, so the gate is a no-op unless a request sets it.

### Server wiring

`app/core/vespa.py::search()` gained `ranking_profile` + `min_rank` kwargs that
plumb straight to Vespa query params (`ranking=…`,
`ranking.features.query(min_rank)=…`). The NIP-50 relay
(`app/routers/nip50/router.py`) maps tokens → profile:

| Client tokens | `ranking_profile` | `min_rank` |
|---|---|---|
| (none) | _default_ | — |
| `filter:rank:gte:50` | `rank_filtered` | `50` |
| `filter:rank:gt:50` | `rank_filtered` | `50 + ε` |
| `sort:rank:desc` (± filter) | `rank_desc` | filter value or — |
| `sort:rank:asc` (± filter) | `rank_asc` | filter value or — |

`_apply_filters_and_sort`, `EXTENSION_OVERFETCH`, and `fetch_hits` are removed —
results are emitted in the order Vespa returns them.

> **`sort:rank:asc` + zero scores.** The default search drops `user_score ≤ 0`
> hits. For ascending sort that would hide the lowest-trust docs the user
> explicitly asked to see first, so the relay passes
> `include_zero_score_results=True` only for `sort:rank:asc`.

### Limitation — only `gte` / `gt` are supported natively

`rank-score-drop-limit` is a **lower-bound** mechanism (a `≥` gate), so only
`gte` and `gt` map to it. `gt:N` is implemented as `min_rank = N + ε` (and since
`rank` is an integer 0–100, this is just `≥ N+1`).

`lte` / `lt` / `eq` are **intentionally unsupported** and the relay returns a
NIP-01 `NOTICE` when a client sends them. Supporting them natively would require
re-modeling per-observer scores as a stored, range-queryable structure
(e.g. `array<struct{observer, score}>` with `sameElement`, queryable in YQL
`where`) — a new schema field + a dual write path alongside the ranking tensor +
a **full re-feed**. Not worth it until there is real demand and more than one
per-observer metric.

---

## Deploying schema changes

Both `doc.sd` edits above are **rank-config only** — they do not touch the
`document doc { … }` field block. That means:

- **Redeploy reloads the rank config; no re-feed / reindex is needed.** Existing
  docs and the `quality_scores` tensor are untouched.
- Deploy by applying the Helm chart in `nosfabrica-kube` (the post-upgrade Job
  re-POSTs the app package). Manual recipe: `charts/brainstorm/VESPA.md`.

A change that *would* force a re-feed: adding/altering a field inside
`document doc { … }` (e.g. the `array<struct>` re-model mentioned above).

### One thing to verify live

The docs don't pin down whether `rank-score-drop-limit` drops on `<` or `<=`.
The sentinel gap makes it moot for real hits, but if you want certainty: run a
query with `min_rank` set high against a known low-score doc and confirm it's
absent.
