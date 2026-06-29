# How tapestry searches (Meilisearch) vs. us (Vespa) — what to learn

_Last updated: 2026-06-29. Owner: search. Audience: the team. Purpose: the team
asked us to mimic tapestry's Nostr-profile search with Vespa; some testers feel
Meilisearch returns better results. This explains **why** the results differ and
**what is worth porting** to our Vespa rank profile._

> Cross-refs: [`search-precision-and-filtering.md`](search-precision-and-filtering.md)
> (text-vs-text fixes) and [`search-trust-vs-exact-match.md`](search-trust-vs-exact-match.md)
> (the additive-trust swamping bug). This doc is the layer above those: a
> head-to-head with tapestry's implementation.

Tapestry source explored: `nostr-search/` (the Meilisearch index + search API),
`nip50-proxy/` (the NIP-50 relay), `src/algos/nip85/` and
`src/api/search/profiles/meili/` (WoT score loading), plus
`nostr-search/BIBLE.md` and `engineering-team/epics/search-quality.md`.

---

## TL;DR

1. **The single biggest difference: tapestry never blends trust into text
   relevance.** Meilisearch ranks purely on text; Web-of-Trust scores are a
   separate numeric **sort/filter** dimension applied in a two-phase search. Our
   Vespa profile *adds* `quality_boost` (0–1000) into a ~100-scale text score, so
   trust distorts ordering. This is the same swamping pathology documented in
   `search-trust-vs-exact-match.md` — tapestry's architecture cannot have it.
2. **Exactness and field-priority are first-class ranking rules** in Meilisearch,
   not hand-tuned constants. We re-implement them by hand and it's brittle.
3. **Tapestry searches more fields** (`nip05`, `npub`, `username`, `lud16`,
   `website`), does **NIP-05 `.well-known` verification**, and uses **length-gated
   typo tolerance + proximity**. We do none of these yet.
4. The way to know whether we've closed the gap is an **offline eval harness**
   (recall@k / MRR over a judged gold set) — which tapestry is also building.
   "Feels better" is currently anecdotal on both sides.

---

## 1. The architecture difference (the important part)

### Tapestry: text and trust are orthogonal

Meilisearch index settings (`nostr-search/src/ingest.js:38-66`):

```javascript
searchableAttributes: [           // ORDER = field weight (name highest)
  'name', 'display_name', 'displayName', 'username',
  'nip05', 'npub', 'about', 'lud16', 'website',
],
rankingRules: [                   // stock lexicographic ladder
  'words', 'typo', 'proximity', 'attribute', 'sort', 'exactness',
],
typoTolerance: { enabled: true, minWordSizeForTypos: { oneTypo: 3, twoTypos: 6 } },
```

WoT scores are **not** in those rules. They are stored as separate numeric fields
namespaced per observer — `wot_rank_<8charPovSuffix>`, `wot_followers_<suffix>`
(`src/algos/nip85/loadScoresIntoMeilisearch.js:88-99`) — registered as
`filterable`/`sortable` at score-load time, and applied via a **two-phase search**
(`nostr-search/src/search.js:76-140`):

- **Phase 1** — run the text query over WoT-scored profiles (optionally filtered
  to `score > 0`), then **re-sort that set by the trust field** in JS (Meilisearch
  `sort` is only a tiebreaker within text tiers, so they re-sort to force order).
- **Phase 2** — if no filters are set, **backfill** with unscored text matches so
  non-scored profiles are still discoverable, ranked after the scored ones.

The NIP-50 proxy defaults to `sort:followers:desc` when the client sends no sort
token (`nip50-proxy/src/search.js:111-166`). So tapestry's **default ordering is
"text matches, sorted by trust within"** — never "text score + trust score."

### Us: text and trust are added together

Our default Vespa profile `name_and_quality_score_only`:

```
relevance() = has_token_match()*1100 + primary_text() + secondary*about + quality_boost()
                                        └── ~0..200 ──┘                    └── 0..1000 ──┘
```

`quality_boost()` is additive and ~10× the text scale. The `has_token_match` tier
(added in `search-trust-vs-exact-match.md`) keeps real matches above trigram
noise, but **within the matched set, trust still dominates ordering** — which is
*not* what Meilisearch does.

**Why this makes their results "better":** exact/text relevance is never distorted
by trust. Trust only reorders *within* an already-text-correct result set. We're
still mixing the two signals on one axis.

---

## 2. Side-by-side

| Dimension | tapestry (Meilisearch) | us (Vespa) |
|---|---|---|
| Text ranking | Stock ladder: words→typo→proximity→attribute→sort→exactness | Hand-rolled `matchCount*100 + bm25/len + grams` |
| Trust integration | **Separate** numeric field; sort/filter; two-phase | **Additive** `quality_boost` in `relevance()` |
| Exact-match priority | `exactness` ranking rule (structural) | `has_token_match()*1100` tier (manual) |
| Field priority | `searchableAttributes` order (name>…>about) | `primary_text = max(name,display_name)`, about gated |
| Searchable fields | name, display_name, username, **nip05, npub**, about, **lud16, website** | name, display_name, about (+ `*_gram`) |
| Typo tolerance | Length-gated: ≥3→1 typo, ≥6→2 | Fuzzy `maxEditDistance:1, prefixLength:2` + **trigram OR firehose** |
| Prefix / search-as-you-type | On by default | `prefix:true` userInput clause |
| Proximity (multi-word) | `proximity` ranking rule | None (per-word OR groups + CamelCase-join variant) |
| NIP-05 verification | Parallel `.well-known/nostr.json` lookup + surface verified hit | None |
| Per-observer scores | `wot_<metric>_<suffix>` fields; **mutates index settings at runtime**; **prunes unscored** to bound size | Sparse `quality_scores` tensor `tensor<int8>(user{})` — no schema mutation, no pruning |
| Dedup | pubkey primary key, keep latest `created_at` | pubkey docid, partial-update upsert |
| Broad-query robustness | Must **catch a Meilisearch panic** on queries like "primal" and return "too broad" | Handles broad queries fine |
| Quality measurement | Building recall@k / MRR eval harness (judged gold set) | None yet |

---

## 3. Why results are "slightly different" (concrete causes)

1. **Ordering** differs because we add trust to relevance and they sort by it
   separately. Same matches, different order.
2. **Recall** differs: a query that hits `nip05`/`npub`/`username`/`website`
   finds the profile in tapestry, not in our index.
3. **NIP-05** queries (`name@domain`) surface a verified profile at the top in
   tapestry; we treat it as plain text.
4. **Typo candidate sets** differ: length-gated typos vs. our trigram-OR recall
   pull in different neighbours, which shifts both recall and order.
5. **Multi-word** queries: their `proximity` rule rewards token closeness; we have
   no proximity signal.

---

## 4. What to port to Vespa (prioritized)

| # | Change | Vespa mechanism | Effort | Impact |
|---|---|---|---|---|
| **P0** | **Stop adding trust into relevance.** Default profile = pure text + exactness tier; expose trust only as sort/filter (we already have `rank_desc` / `rank_filtered`). Mirrors tapestry's two-phase. | Remove `quality_boost()` from `relevance()`; keep the `has_token_match` tier; route trust through the `rank_*` profiles / a `second-phase` | Low–med | **Highest** |
| **P1** | Index `nip05, npub, username, lud16, website` as searchable; add to YQL word groups | schema fields + `_word_group` in `app/core/vespa.py` | Low | High (recall parity) |
| **P2** | NIP-05 `.well-known/nostr.json` verify + surface the verified hit (dedup) | new path in `app/routers/search/router.py` | Med | Med |
| **P3** | Align typo budget to length-gated (≥3→1, ≥6→2); lean less on grams | `_word_max_edits`, lower `query(w_gram)` | Low | Med |
| **P4** | Add proximity for multi-word | `nativeProximity` / `fieldMatch` term in the rank profile | Med | Med |
| **P5** | **Search-eval harness** (recall@k / MRR over a hand-judged gold set, ~30 queries) | offline test; reuse `scripts/search_*.sh` to capture rankings | Med | **Process** |

**P0 is the highest-leverage change** and directly removes the swamping that
`search-trust-vs-exact-match.md` is about. **P5 is what makes "is it actually
better?" answerable** instead of anecdotal — tapestry is building the same harness
(`engineering-team/epics/search-quality.md`) for exactly this reason.

---

## 5. Where we're already equal or better (don't over-correct)

- **Per-observer trust storage.** Our sparse `quality_scores` tensor is cleaner
  than tapestry's `wot_<metric>_<suffix>` fields, which require mutating the
  index's `filterable`/`sortable` settings at runtime per observer and **pruning
  unscored profiles** to keep the index small. We need neither.
- **Robustness.** Tapestry has to catch a Meilisearch v1.12.8 interner panic on
  broad queries (e.g. "primal") and return a "query too broad" notice
  (`nostr-search/src/search.js:144-170`). Vespa handles those queries.
- **Diacritics / case.** Vespa's default linguistics already lowercases and
  normalizes accents on indexed string fields — comparable to Meilisearch's
  tokenizer, so this is not a differentiator.

---

## 6. The one architecture decision for the team

Tapestry's default NIP-50 ordering is `sort:followers:desc` — text matches sorted
by trust *within*. That is the clean version of what our additive `quality_boost`
was clumsily approximating. So we need to choose our **default** ordering:

- **Pure text relevance** (like tapestry's `/api/search` with no sort), or
- **Trust-sorted-within-text** (like tapestry's NIP-50 default).

Recommendation: **pure-text default for `/search/byText`, trust-sort for the
NIP-50 relay** — matching tapestry exactly. This decides whether P0 means "drop
`quality_boost` entirely" or "move it to a second-phase sort."

---

## 7. Suggested next steps

1. Decide the default-ordering question in §6.
2. Prototype P0 on a branch; A/B against the current profile on staging using
   `scripts/search_compare.sh` and `scripts/search_http.sh`.
3. Land P1 (more searchable fields) — cheapest recall win; needs a reindex/refeed
   only for the new fields.
4. Stand up the P5 eval harness with a small judged gold set so every later change
   is measured, not guessed.

### Reference: key tapestry files

| Concern | File |
|---|---|
| Index settings / ranking rules | `nostr-search/src/ingest.js:38-66` |
| Document fields | `nostr-search/src/ingest.js:130-149` |
| Two-phase search + re-sort | `nostr-search/src/search.js:76-140` |
| Broad-query panic workaround | `nostr-search/src/search.js:144-170` |
| WoT score load / namespacing | `src/algos/nip85/loadScoresIntoMeilisearch.js:88-99` |
| NIP-05 verify + dedup | `src/api/search/profiles/meili/index.js:26-46, 209-214` |
| NIP-50 parse / execute | `nip50-proxy/src/search.js:25-62, 111-166` |
| Design rationale | `nostr-search/BIBLE.md:201, 305-318`; `engineering-team/epics/search-quality.md` |
