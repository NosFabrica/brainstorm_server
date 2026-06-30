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

---

## 8. Agreed implementation plan (team decisions, 2026-06-29)

We are proceeding with **P0 + P1**. The default behavior below **supersedes §6's
"pure-text default for /search/byText"** — the team chose a popularity-first
default ("first-time users expect popular accounts at the top").

### 8.1 Default ranking — `text → filter rank≥2 → sort by verified followers`

The SAME default for both `/search/byText` (UI) and the NIP-50 relay:

1. **Text match** (existing recall + the `has_token_match` tier so genuine
   matches beat trigram noise).
2. **Filter `rank ≥ 2`** at query time via `query(min_rank)=2` (configurable
   per query). `rank` = the observer's GrapeRank score (`influence×100`, 0..100),
   i.e. the existing `quality_scores` tensor.
3. **Sort by verified-follower count** (`trusted_followers`), descending.

"Order by rank" was considered; verified-follower count was chosen as the
default because it maps to "popular" more intuitively. **Sorting is
configurable** via a `sort=` param (byText) / `sort:` token (NIP-50):

| `sort=` | profile | sort key (filter is always `rank ≥ min_rank`) |
|---|---|---|
| `followers` (default) | `sort_followers` | `has_token_match*1100 + verified_followers()` |
| `rank` | `rank_desc` | `has_token_match*1100 + user_score()` |
| `text` | `text_relevance` (the P0 profile) | `relevance()` (pure text) |

So **P0 isn't discarded** — `text_relevance` becomes the `sort=text` option, and
P0's architecture (text & trust as separate dimensions, the match-quality tier)
is what makes this clean. `DEFAULT_RANK_PROFILE` moves from `text_relevance` to
`sort_followers`.

### 8.2 New metric: verified-follower count in Vespa

`scorecard.trusted_followers` is already computed and **already published in the
kind-30382 TA events** (`upload_nostr_events.py:105`) — it's just never sent to
Vespa. To sort on it:

- **Schema:** add `field follower_counts type tensor<float>(user{})` (per-observer,
  same shape as `quality_scores`). Must be `float`, not `int8` — counts exceed
  int8's 127 ceiling; Vespa tensors only support int8/bfloat16/float/double.
- **Pipeline:** `upsert_scores_to_vespa` pushes a second cell
  (`trusted_followers`) alongside the existing rank cell.
- **Rank profile:** `verified_followers() = sum(query(user_q) * attribute(follower_counts))`.

### 8.3 Vespa ingest threshold — index scores **> 0** (not `== 0`)

Decouple the **Vespa** ingest/removal threshold from the **Nostr TA publish**
cutoff (`cutoff_of_valid_graperank_scores`, 0.05). The TA publish/delete cutoff
stays as-is (0.05); Vespa indexes **everything with rank > 0** so it's all
searchable, and the `rank ≥ 2` *default filter* (§8.1) decides visibility at
query time.

Implementation note — the cutoff currently drives THREE things
(`upload_nostr_events.py`): TA publish (line 98), Vespa ingest (line 280), and
the deletion set (lines 341-346) which feeds BOTH Nostr kind-5 deletes and Vespa
`removes`. So ">0 ingest" requires:

- **Ingest:** line 280 gate → `round(influence*100) <= 0: continue` (was `< cutoff`).
- **Removal:** split the single `pubkeys_to_delete` set. Nostr deletes stay
  cutoff-based; the **Vespa remove set must be rank==0 / gone-from-scorecards
  only**, or rank 1-4 profiles get ingested then immediately swept out.

### 8.4 P1 — more searchable fields

Add searchable: `username`, `nip05`, `lud16`, `website`. **Skip `npub`** — the
router already resolves npub/hex to a direct doc fetch (`_try_resolve_pubkey`),
so indexing it only adds marginal partial-match value.

- `nip05`, `lud16`, `website` are already stored (`indexing: summary`) → flip to
  `index | summary` → **Vespa reindex** (rebuilds from the doc store, no strfry
  re-feed).
- `username` is not in Vespa at all → ingest change (extract from kind-0
  content) **+ a kind-0 re-feed** from strfry.

#### 8.4.1 kind-0 ingest robustness — read BOTH content and tags

We currently ingest kind-0 at `process_strfry_event.py:process_event_kind_0`,
which parses **only `event["content"]` (the JSON-encoded field) and ignores
`event["tags"]`**. Newer clients mirror the same profile fields as tags
(`["name", …]`, `["display_name", …]`, `["nip05", …]`, …) — sometimes *in
addition to* content, sometimes as the primary source.

Two concrete bugs this creates:

1. **Wipe risk (correctness, not just recall).** `upsert_profile` is a *replace*
   that assigns `""` to any `PROFILE_FIELDS` key missing from the new event. A
   tag-only kind-0 with empty/minimal `content` parses to `{}` and would
   **blank out** that profile's searchable fields in Vespa instead of being
   ignored.
2. **camelCase variants dropped.** Clients send `displayName` (camelCase) and
   `username`; we only read `display_name` and don't read `username` at all.

Fix (build with P1, not a JSON-only `username` add):
- Merge sources: build the profile dict from `content` JSON **and** from the
  `[key, value]` profile tags, with a precedence rule (content wins; tags fill
  gaps).
- Normalize aliases: `displayName` → `display_name`, etc.
- **Skip-empty guard:** if the merged result has *no* recognized profile fields
  (truly empty/malformed event), skip the upsert entirely — `upsert_profile`
  clears missing fields to `""`, so an empty event would otherwise wipe a good
  profile. (We deliberately do *not* do a per-field "don't clear" guard: kind-0
  is a replaceable full-profile event in Nostr, so a field genuinely omitted by
  a complete event *should* clear. The merge makes tag-only events non-empty, so
  they no longer trigger the wipe.)
- Then extract the expanded `PROFILE_FIELDS` (now including `username`).

### 8.5 Backfill / deploy — one maintenance window

The schema + server deploy together; then two independent backfills (tooling now
exists for both):

| Work | Backfill | Mechanism |
|---|---|---|
| follower_counts tensor (§8.2) + >0 ingest (§8.3) | score re-sync | `scripts/trigger_graperank_all.py` (all observers, paced) — or just the default observer for anonymous search |
| P1: username (§8.4) + the nip05/lud16/website indexes | profile re-feed | `scripts/refeed_kind0_to_vespa.py` (replays kind-0 through the live ingest path) |

The `summary→index` change on nip05/lud16/website requires
`vespa-app/validation-overrides.xml` (`indexing-change`) — see §9.1. Once the
change is accepted, Vespa can also reindex those three from its doc store, but
the kind-0 re-feed (run for `username` anyway) rebuilds them regardless.

Deploy schema **with** the server — `DEFAULT_RANK_PROFILE = "sort_followers"`
references a profile the running Vespa must already have.

### 8.6 Build order

1. Schema: `follower_counts` tensor + `sort_followers` profile + P1 `index`
   fields (doc.sd, nosfabrica-kube).
2. Pipeline: push `trusted_followers`; change ingest/removal to >0 (split the
   delete set).
3. API: `DEFAULT_RANK_PROFILE = "sort_followers"`; add `sort=` to `/search/byText`;
   add `followers` metric to the NIP-50 `sort:`/`filter:` map.
4. P1 ingest: extract `username`; add the new searchable fields to the YQL groups.
5. Tests + A/B on staging with `scripts/search_*.sh`; then the §8.5 backfill.

---

## 9. Deploy runbook

**Status (2026-06-30):** server code + tests committed; the two backfill scripts
(`scripts/trigger_graperank_all.py`, `scripts/refeed_kind0_to_vespa.py`)
committed. In nosfabrica-kube (uncommitted, deploy-time): `doc.sd` **and**
`vespa-app/validation-overrides.xml`. Validated on an alternate staging that the
`summary→index` change is rejected without the override (now added — §9.1). No
downtime at any step; schema changes are online, backfills are background.

### 9.0 What each change does to Vespa (and how it's populated)

| Schema change | Vespa behavior | Populated by |
|---|---|---|
| New rank profiles (`sort_followers`, edited `rank_*`, `text_relevance`) | online, instant | — |
| Extended `has_token_match` / `secondary_active` (matchCount username/nip05) | online (rank expr) | — |
| New attribute `follower_counts` (tensor<float>) | online; **empty** for existing docs | **score re-sync** (§9.2 step 4) |
| New field `username` (indexed) | online; **empty** for existing docs | **kind-0 re-feed** (§9.2 step 5) |
| `nip05`/`lud16`/`website` `summary` → `index\|summary` | **`indexing-change`** — rejected without `validation-overrides.xml`; index empty for existing docs until reindexed/re-fed | reindex from doc store and/or kind-0 re-feed |

No change drops data or needs a reindex-from-scratch.

### 9.1 Prerequisite: the `indexing-change` override (validated, now in place)

The first deploy attempt to staging failed with:

```
INVALID_APPLICATION_PACKAGE  indexing-change:
  Field 'nip05'/'lud16'/'website' changed: add index aspect
  ... To allow this add <allow until='yyyy-mm-dd'>indexing-change</allow> to validation-overrides.xml
```

This is Vespa's guardrail for turning a `summary` field into an indexed one (it
implies reindexing), **not** a schema bug. Fix, added at
`charts/brainstorm/vespa-app/validation-overrides.xml`:

```xml
<validation-overrides>
    <allow until="2026-07-14">indexing-change</allow>
</validation-overrides>
```

- **Auto-included** via the ConfigMap's `.Files.Glob "vespa-app/**"`
  (`templates/vespa-app-deploy.yaml`) — no template change; lands at the package
  root where Vespa expects it.
- `until` must be **≤ 30 days** out; bump it if you deploy later than that.
- **Remove it (or let it expire) once the change is live everywhere** — a
  lingering override would silently wave through *future* accidental indexing
  changes. Treat removal as a post-deploy cleanup task.

The other schema changes (new `follower_counts`/`username` fields, new rank
profiles) are additive and need no override.

### 9.2 Sequence (staging first, verify, then prod off-peak)

1. **Pre-flight:** recent `brainstorm-backups-vespa` run; `vespa.appPackage.enabled=true`;
   `vespa-app/` contains both `doc.sd` and `validation-overrides.xml`.
2. **Deploy schema + server together** (`helm upgrade`, per `charts/brainstorm/VESPA.md`).
   The post-upgrade hook zips `vespa-app/` (now incl. the override) and POSTs to
   `:19071/.../prepareandactivate`. **Watch**
   `kubectl logs job/<release>-brainstorm-vespa-app-<rev>` — it should now activate.
   - *Impact:* byText/NIP-50 immediately use `sort_followers`, but
     `follower_counts` is empty → ordering within a tier is flat (ties at 0) until
     step 4. The `rank≥2` filter works immediately (reads existing
     `quality_scores`). name/display/about search is unaffected.
   - **Seconds-long window:** if the server pod is ready before the app-package
     activates, byText errors on "unknown rank profile `sort_followers`" — it
     self-heals when the Job activates. To avoid it entirely, activate the schema
     *before* rolling the server (manual `prepareandactivate`, then the upgrade).
3. **(Optional) confirm reindex** of nip05/lud16/website from the doc store via the
   config server's reindexing status. Step 5's re-feed rebuilds them regardless,
   so this is belt-and-suspenders.
4. **Score re-sync** → populates `follower_counts` + applies `>0` ingest:
   - *Default experience (enough for anonymous/popular-first):* trigger the
     default observer — `POST /admin/brainstormPubkey/{pubkey}/trigger_graperank`
     or the periodic cronjob.
   - *All personalized perspectives:* `python -m scripts.trigger_graperank_all
     --status`, then `--rate N [--limit N]` (paced/resumable; tracks done via the
     `brainstorm_request` table).
   - *Impact:* bounded burst of partial-update writes (more rows: >0 vs ≥0.05);
     GrapeRank itself is the heavy part — pace it. Search stays up.
5. **kind-0 re-feed** → populates `username` + rebuilds nip05/lud16/website:
   `python -m scripts.refeed_kind0_to_vespa --status`, then `--concurrency N
   [--limit N]` (resumable via its cursor).
   - *Impact:* one partial-update per profile; `quality_scores`/`follower_counts`
     tensors are preserved; skip-empty + content/tags merge means nothing gets wiped.
6. **Verify** with `scripts/search_http.sh` / `search_compare.sh` / `search_nip50.sh`:
   popular-first ordering, `rank≥2` exclusion, `username`/`nip05` matches, and the
   `sort=rank` / `sort=text` alternates.

### 9.3 Rollback

`helm rollback` (or redeploy the prior chart) reverts the rank profiles and the
server. The added `follower_counts`/`username` fields and any written cells are
**harmless to leave** — the previous profiles don't read them, and no data is
lost. The `summary→index` change is the only one that isn't a clean auto-revert
(removing an index aspect would itself need an override); prefer rolling *forward*
(fix + redeploy) over reverting that specific field.

### 9.4 Degraded-but-functional windows (summary)

| Between… | Search still works? | What's missing |
|---|---|---|
| schema deploy → score re-sync | yes | follower ordering is flat (ties at 0); `rank≥2` filter already works |
| schema deploy → reindex/re-feed | yes | `nip05`/`lud16`/`website` matches (reindex from store), `username` matches (re-feed) on existing profiles |
| server ready → app-package active | ~no (seconds) | byText errors on unknown profile until the Job activates |

### 9.5 Post-deploy cleanup

- Remove `vespa-app/validation-overrides.xml` (§9.1) once the indexing change is
  activated everywhere it needs to be, and redeploy.
- Once the index is in steady state, consider flipping `VESPA_FULL_SYNC` to
  `False` (`upload_nostr_events.py`) so routine GrapeRank runs only push *changed*
  scores instead of the full set.

### 9.6 kind-0 repopulation on an already-running namespace

Pulls the new P1 fields (`username`, indexed `nip05`/`lud16`/`website`) into Vespa
for existing docs via `scripts/refeed_kind0_to_vespa.py`. **Schema must already be
deployed** (§9.2 step 2) or every upsert fails on an unknown field.

Runs as the suspended `vespa-kind0-refeed` CronJob (chart:
`templates/vespa-kind0-refeed.yaml`) in its own pod — own resources, durable
logs, no load on the live API server. Trigger on demand and tail the logs
(the script prints per-page progress):

```bash
export KUBECONFIG=~/.kube/configs/<stage>.conf   # e.g. arrowhead-admin.conf
NS=<namespace>                                    # e.g. staging | arrowheadstaging

kubectl -n "$NS" create job vespa-kind0-refeed-now \
  --from=cronjob/brainstorm-vespa-kind0-refeed
kubectl -n "$NS" logs -f job/vespa-kind0-refeed-now
```

Knobs: `vespaKind0Refeed.concurrency` / `.page` in values. Idempotent; a retry
restarts from newest (the resume cursor is pod-local, lost on restart). Trust /
follower-count backfill is separate (`trigger_graperank_all.py`, §9.2 step 4).

Verify (expect ≥1 hit where there were none before):

```bash
kubectl -n "$NS" exec brainstorm-vespa-0 -- curl -s \
  'http://localhost:8080/search/?yql=select%20pubkey%2Cusername%20from%20doc%20where%20username%20contains%20%22<username>%22&hits=5'
```

Quick one-off without the Job (runs inside the live server pod — fine for a small
stage, avoid on a large one):

```bash
POD=$(kubectl -n "$NS" get pod -l app.kubernetes.io/component=brainstorm-server -o jsonpath='{.items[0].metadata.name}')
kubectl -n "$NS" exec "$POD" -- sh -c 'cd /app && poetry run python -m scripts.refeed_kind0_to_vespa --status'
```
