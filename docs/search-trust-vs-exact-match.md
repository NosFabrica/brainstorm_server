# Search ranking: trust swamps exact matches

_Last updated: 2026-06-28. Owner: search. Audience: the team — read this before
touching the `name_and_quality_score_only` rank profile in `doc.sd`._

This note is a follow-on to `search-precision-and-filtering.md`. That doc fixed
**text-vs-text** ordering (trigram/fuzzy recall outranking exact token matches).
This one is about the next layer up: **text-vs-trust** — the per-observer
quality score (`quality_boost`) is so much larger than any text signal that it
swamps it entirely, so genuine name matches from low-trust accounts fall off the
end of the result set.

> **Where the schema lives.** The *deployed* schema is
> `nosfabrica-kube/charts/brainstorm/vespa-app/schemas/doc.sd`, shipped via the
> Helm ConfigMap + post-install/upgrade Job (see `charts/brainstorm/VESPA.md`).
> The copy under `brainstorm_one_click_deployment/vespa-app/` is **stale** —
> treat the kube repo as source of truth.

---

## Symptom

A test account **"Handled"** (`npub1m2lrsze…`, hex `dabe380b…`) does not appear
in search results for the exact query `handled`, even though its `name` and
`display_name` are exactly "Handled". The account is real, indexed, and has a
(low) trust score. "It should be far down, but it should be *there*" — it isn't.

## Investigation (staging, 2026-06-28)

Three candidate causes, checked in order against staging Vespa
(`kubectl exec -n staging brainstorm-vespa-0 -- curl localhost:8080/...`):

1. **Filtered out by `onlyRanked`?** No. `/search/byText` defaults
   `onlyRanked=true`, which drops any hit whose `user_score ≤ 0` for the search
   observer. The doc's `quality_scores` tensor **has a cell for the default
   observer** (`be7bf5de…`) with value `11` → `user_score = 11 > 0` → it passes
   the filter.

2. **Recall miss (not matched by the YQL)?** No. It's an exact token match on
   both `name` and `display_name`.

3. **Buried by ranking?** **Yes.** Direct Vespa rank breakdown for the doc under
   the default observer, profile `name_and_quality_score_only`, query `handled`:

   ```
   relevance:          165.9
     name_text:        110.5   ← exact token match (matchCount 1 ×100 + bm25)
     display_name_text:111.1   ← primary_text = max(name, display_name)
     about_text:         0.0
     quality_boost:     54.8   ← from score 11
     user_score:        11.0
   ```

   For comparison, the live `handled` result set (`maxHits=400`):

   | Rank | Account         | relevance | quality score |
   |------|-----------------|-----------|---------------|
   | 1    | greencandleit   | 1053      | 94            |
   | 2    | gandlaf21       | 1043      | 97            |
   | 3    | kenn3d          | 1030      | 98            |
   | …    | …               | …         | …             |
   | 400  | ninjagrandma    | 895       | —             |
   | ~?   | **Handled**     | **166**   | **11**        |

   **None of the top results are the word "handled."** They are trigram-only
   matches (`han` / `and` / `led` are common substrings — Alex**and**er,
   g**and**laf, green**c**andleit…) riding on a near-maximal `quality_boost`.
   Handled, at relevance 166, sits *hundreds* of places below the 400-hit
   window, so the client never sees it.

## Root cause

The ranking blend is **additive**, and the two terms are on wildly different
scales (`doc.sd`, profile `name_and_quality_score_only`):

```
relevance() = primary_text()  +  secondary_active()*w_about*about_text()  +  quality_boost()
              └─── 0 … ~200 ───┘                                            └─── 0 … 1000 ───┘
```

- `primary_text()` for an **exact single-word** name match ≈ **110**
  (`matchCount ×100` + small bm25). The per-field trigram contribution is capped
  at `gram_cap = 80`.
- `quality_boost()` is a logistic sigmoid of the observer's score, ranging
  **0 … 1000** (σ centered at score 50).

So the entire text signal (exact vs. trigram-only ≈ 110 vs. 80, a 30-point
spread) is dwarfed by the ~1000-point trust term. Ranking is effectively
"**sort by trust; text barely matters.**" The recall net is also broad — the
trigram OR pulls in every high-trust account that shares *any* 3-char substring
with the query — and additive trust then floats all of them above the real
match.

> The `gram_cap (80) < matchCount (100)` invariant noted in
> `search-precision-and-filtering.md` only holds **at equal trust**. It says
> nothing about the additive `quality_boost`, which is ~10× larger than the
> whole text score and is what actually dominates here.

## What we actually want

Agreed product behavior (do **not** confuse with "exact match must be #1"):

- **Trust/rank leads at the top.** Among genuine matches, the highest-trust
  account ranks first. We want trust to drive the top of the list.
- **A genuine name match must outrank a trigram-only accident.** "handled"
  results should be accounts actually named/about "handled," not every
  high-trust account that happens to contain `and`.
- **Low-trust exact matches still appear** — below high-trust exact matches, but
  above the trigram-only noise.

Restated as a ranking rule:

> **Match quality is the primary partition; trust orders within each partition.**
> Exact/strong text match → top tier (ordered by trust). Trigram-only → lower
> tier (ordered by trust). Trust must not let a lower tier jump a higher one.

## Options

### Why plain two-phase is *not* enough

A first-phase=`primary_text()`, second-phase=`relevance()` split was the first
idea. It fails: second-phase still uses the additive, trust-dominated
`relevance()`, so once the trigram-only high-trust accounts survive into the
re-rank set they re-bury the exact match exactly as before. Two-phase changes
*which* docs get re-ranked, not the *formula* that orders them. The formula is
the problem.

### Option A — Tiered/gated additive (recommended)

Gate on a real token match (which trigram-only matches do **not** have —
`matchCount` counts `name`/`display_name` index hits, not `*_gram` hits) and add
a constant large enough to clear the `quality_boost` ceiling:

```
has_token_match = if(matchCount(name) > 0 || matchCount(display_name) > 0, 1, 0)
relevance() = has_token_match * TIER + primary_text() + secondary*about + quality_boost()
```

With `TIER = 1100` (just above the 1000 `quality_boost` max):

| Doc                         | tier  | text | quality | total |
|-----------------------------|-------|------|---------|-------|
| High-trust exact match (100)| 1100  | 111  | ~1000   | ~2211 |
| **Handled** (exact, 11)     | 1100  | 111  | 55      | ~1266 |
| greencandleit (gram, 94)    | 0     | 80   | ~970    | ~1050 |

Result: all genuine matches sit above all trigram-only matches; **within** each
tier `quality_boost` still orders (trust leads at the top). Handled appears,
below high-trust exact matches, above the noise. Minimal, well-understood
change; preserves the existing trust-ranking behavior wholesale.

_Open sub-question:_ fuzzy/prefix matches also increment `matchCount`, so they
land in the same top tier as exact. That's probably fine (and is the
`prefixLength:2`, `maxEditDistance:1` budget from Problem 1), but if we want
exact strictly above fuzzy we add a second, smaller tier constant.

### Option B — Multiplicative blend

Make text a gate by multiplying instead of adding (cf. the unused `search_rank`
profile): `relevance = text_score * (1 + k·user_score)`. Conceptually clean —
zero/low text can't float on trust — but the raw text spread (exact 110 vs
trigram 80) is too small to separate cleanly *because trigram matches still
produce non-trivial text*, so it needs the same token-match gate as Option A to
work well. More re-tuning, more global behavior change. Lower confidence.

### Option C — Shrink `quality_boost` to a tie-breaker

Cap `quality_boost` at, say, 80 so text leads. One-liner, but it throws away
trust-led ranking at the top — directly contradicts "trust leads at the top."
Rejected.

## Recommendation

**Option A.** It satisfies all three product requirements with the smallest,
most legible change, and keeps trust as the ordering signal exactly where we
want it. Validation plan: add the `has_token_match` tier to
`name_and_quality_score_only` in `doc.sd`, redeploy to staging via the
app-package hook, and re-run the `handled` query — expect Handled to surface
below the high-trust exact matches and above the trigram noise. Add `matchCount`
to `match-features` during validation so we can see the tier per hit.
