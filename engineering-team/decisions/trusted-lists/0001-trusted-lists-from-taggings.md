# ADR 0001: Trusted Lists from pubkey Taggings

**Status:** Proposed
**Date:** 2026-08-25
**Story:** `engineering-team/stories/trusted-lists/1-trusted-lists-from-taggings.md`
**Issue:** [NosFabrica/brainstorm_server#73](https://github.com/NosFabrica/brainstorm_server/issues/73)

## Context

Issue #73 asks for admin-triggered generation and publication of one kind-30392
Trusted List per entry in a customer's "dictionary" of pubkey Tags, where the
dictionary is defined by *usage*: any Tag applied at least once by a verified
(Rank > 2) asserter qualifies. Tapestry has shipped the equivalent capability;
this ADR decides what ports, what changes, and why.

### The reference implementation

Verified by reading `nous-clawds4/tapestry` at `buzz-communities-integration`:

- **`src/api/profile-tags/index.js`** (1680 lines). `aggregateProfilesTagged`
  (line 571) is the membership aggregator: scan kind-39999 taggings for a tag,
  replaceable-dedupe, filter asserters by `wot_rank_<povSuffix> >= minRank`,
  bucket by polarity into per-target applications/disputes.
  `handleTagIndex` (line 924) is the dictionary: the same scan grouped by the
  referenced tag element instead of by target.
- **`src/api/trustedList/refreshPinnedTags.js`** (319 lines).
  `applyDisputesFunction` (line 99) is the membership predicate;
  `runOnePin` (line 120) is the per-TL pipeline; `retractStaleTLs` (line 249)
  is the empty-replacement retraction.
- **`src/api/trustedList/index.js`**. `buildAndPublishTL` (line 116) is the
  wire-shape builder and publisher.
- **`src/api/_shared/pov.js`**. `resolvePov` yields `{povSuffix, minRank}`;
  `minRank` is a configured filter floor, compared inclusively (`>=`).
- **Wire contract**, from `protocols/drafts/tags.md` and the deployed code:
  tag elements and taggings are both kind 39999, distinguished by their `z`
  tag; taggings carry `p` (target), `e` (tag element event id), `polarity`,
  and a deterministic `d` making them replaceable.

### The three forced divergences

1. **No pin layer.** Tapestry derives every TL from a Pin event whose
   `curation-method` JSON supplies `observer`, `cutoff`, `includeScoreInTL`.
   #73 skips pins. Those three inputs need another source.
2. **Per-customer signing keys.** Tapestry has one owner TA key. This server
   mints one assistant nsec per observer
   (`app/repos/brainstorm_nsec.py:32`) and signs that observer's kind-30382
   TAs with it (`app/message_queue_tasks/upload_nostr_events.py:473`).
3. **No relay shell.** Tapestry reads taggings with `exec('strfry scan …')`
   from inside the relay container. This server never shells out;
   `app/services/observer_sweep_service.py:8-13` records that REQ recovers
   only ~5% of a large set because strfry caps `maxFilterLimit` at 500. That
   comment exists because the failure was measured, not predicted.

### Repo facts this ADR builds on

- **Ingest path.** An out-of-repo strfry plugin pushes events onto the
  `strfry:events` Redis queue; `consume_strfry_plugin_messages`
  (`message_queue_consumer.py:208`) drains it into `process_strfry_event`
  (`process_strfry_event.py:27`), which dispatches on kind and **silently
  ignores unknown kinds**.
- **Relay backfill.** `nostr_event_transferer.ev_kinds`
  (`nostr_event_transferer.py:20-26`) is a fixed list of five kinds pulled
  from a single `NOSTR_TRANSFER_FROM_RELAY`.
- **Per-observer trust scores live in Neo4j** as `NostrUser` node properties
  named `influence_<observerPubkey>`, already read by
  `get_influence_for_observer` (`app/repos/user_repo.py:205`) and by
  `_get_pubkeys_with_influence` (line 23) via a `node[$key]` parametrized
  lookup.
- **Rank is defined** (`CONTEXT.md`) as the published integer 0–100,
  `round(Influence × 100)`.
- **Vespa is explicitly best-effort** (`CLAUDE.md` "Conventions"): writes are
  mirrored, failures are logged and swallowed. The graph and Postgres are
  source-of-truth.
- **Models** live in `app/db_models/__init__.py` against a single `Base`, with
  a `TimestampMixin`; migrations are Alembic autogenerate.

### Invariants this story must honor

- **POV-first.** Membership is computed per Observer. Two Observers over the
  same tagging data produce different TLs. There is no "the" membership.
- **Filter at view time.** The dictionary and membership are recomputed from
  raw taggings on every run — no precomputed per-observer aggregate table.
- **Source-of-truth discipline.** Reads that decide what gets *signed and
  published* must come from a source-of-truth store, never from the
  best-effort Vespa mirror.

## Decisions

### D1 — Acquire taggings by ingesting and persisting them, not by scanning on demand

**Options considered:**

- **A (chosen) — extend the existing ingest path; persist to PostgreSQL.**
  Add kind 39999 to `process_strfry_event`'s dispatch and to the transferer's
  `ev_kinds`; write tag elements and taggings to two new tables. The
  dictionary and membership then compute in SQL.
- **B — on-demand REQ against the relay at button-press.** Closest to
  tapestry's shape (scan at compute time, no store). **Rejected:** this is
  precisely the pattern `observer_sweep_service.py:8-13` documents as
  recall-broken at scale — strfry's `maxFilterLimit` cap silently returns a
  fraction of the set, and a *silently short* membership list is worse than an
  error, because it publishes a signed assertion that someone is not in a list
  they belong to.
- **C — shell out to `strfry scan`, or add a relay-side HTTP scan endpoint.**
  Exactly tapestry's mechanism. **Rejected:** this server is not co-located
  with the relay (it speaks `ws://` to configured relay URLs and has no
  filesystem or process access to strfry), so it would require a new
  cross-service surface and a deployment coupling the architecture currently
  does not have.

**Consequence accepted:** a persisted store means membership reflects what we
have *ingested*, so an un-synced relay yields a quietly incomplete dictionary.
Mitigated by making the ingest source explicit and by reporting per-tag counts
in the trigger response, so an empty or shrunken result is visible rather than
silent.

### D2 — Two tables, replaceability enforced at write time

`nostr_tag_element` keyed by the addressable coordinate (author + slug), and
`nostr_user_tagging` keyed by (asserter, `d` tag). Both store `event_id` and
`created_at`; both apply latest-wins on `created_at` at upsert, so the "one
live stance per (asserter, target, tag)" rule from `tags.md` is a database
invariant rather than a read-time dedupe pass.

Rejected: storing raw events in one table and deduping at read time (tapestry's
`dedupeReplaceable`). It moves per-run cost into every query and makes the
replaceability rule implicit. The write-time form costs one comparison per
ingest and makes the dictionary a plain aggregate query.

### D3 — Rank comes from Neo4j `influence_<observer>`, not Vespa

**Options considered:**

- **A (chosen) — Neo4j per-observer node property.** Source of truth, already
  per-observer, already parametrized-read by `user_repo`, and carries the raw
  float so the threshold comparison happens before any rounding.
- **B — the Vespa `quality_scores` tensor.** It is per-observer and already
  holds Rank as int8. **Rejected:** Vespa is by written convention a mirror
  whose write failures are swallowed, so it can be silently stale or missing
  cells. Deciding what to sign from a store we have explicitly licensed
  ourselves to let drift is a correctness error, not an optimization.
- **C — re-read the Observer's own published kind-30382 TAs.** Most faithful
  to tapestry (whose Meili columns are themselves TA-derived) and the most
  "decentralized" reading. **Rejected for v1:** it reintroduces exactly the
  relay-recall problem of D1-B for a value we already hold locally.

### D4 — Threshold is an inclusive floor on Rank, configurable, defaulting to the issue's line

Issue #73 says "rank > 2". Tapestry compares `>= minRank`. To avoid an
off-by-one buried in a trust default, this ADR defines a single setting
`TRUSTED_LIST_MIN_RANK` compared **inclusively** (`rank >= min_rank`), with
default **3** — which is exactly `rank > 2`. The comparison is performed on
Influence (`influence >= min_rank / 100`) to avoid a double-rounding
discrepancy against the published Rank.

The dictionary-entry threshold ("used more than 0 times") is a second,
separate setting `TRUSTED_LIST_MIN_TAG_USES`, default 1 — issue #73 §3 names
raising it as an anticipated change, so it should not be a literal.

### D5 — TL wire shape: tapestry's layout, re-slugged for the pin-less derivation, plus `description`

```
kind: 30392
d           tl-tag-<observer[0:8]>-<tagAuthor[0:8]>-<slug>
title       <tag element name>
description <tag element description>        # new vs tapestry; issue #73 §4
metric      tag-membership
observer    <observer pubkey>
source-tag  <tagEventId> <tagAuthorPubkey> <slug>
cutoff      <int>
min-rank    <int>
p           <member pubkey>                  # one per member, ordered
content     {"members":[{"pubkey","endorsements","disputes"}]}
```

`d` deliberately does **not** reuse tapestry's `tl-pin-` prefix: these lists
are not pin-derived, and tapestry's `retractStaleTLs` keys its sweep on that
literal prefix. Using it would make the two derivations collide in any relay
that mirrors both. `metric` changes from `pinned-tag-membership` to
`tag-membership` for the same reason.

Keeping `(observer8, tagAuthor8, slug)` as the identity preserves tapestry's
two useful properties: distinct Observers pinning/deriving the same tag get
distinct slots, and same-slug tags by different authors stay distinct.
**Accepted risk:** 8-char truncation makes collisions possible in principle;
this matches tapestry's deployed shape, and widening it later is a wire break,
so it is called out here rather than discovered later.

### D6 — Sign with the Observer's assistant nsec; publish through the existing TA relay path

TLs for Observer X are signed by X's assistant nsec — the same key authoring
X's kind-30382 TAs — so a consumer resolving X's assistant pubkey finds both
the scores and the lists under one author, and one NIP-19 `naddr` scheme
covers both. This is the direct consequence of divergence 2 and the point
where the tapestry port must be rewritten rather than translated: tapestry's
module-level `TA_PUBKEY` constant becomes a per-run parameter.

### D7 — Retraction is an empty-membership replacement, scoped to this Observer

Per tapestry: republish at the same `d` with zero `p` tags, carrying forward
`title`/`metric`/`observer`/`source-tag` and adding `["status","retracted"]`;
skip slots already carrying that marker. Scoped to the triggered Observer —
a run for X must never retract Y's lists.

Critically, a tag whose publish **failed** counts as current for the retraction
pass. Tapestry learned this the hard way (`refreshPinnedTags.js:216-220`,
ADR tag-stack-merge-hardening/0001 B4a): a transient publish failure must
never cause the retraction sweep to wipe the healthy TL still on the relay.

### D8 — The concept `z` addresses are a cross-repo constant, copied verbatim

Taggings are identified by
`39998:82b75e474dda005e912bcbb910391c60c2b89cc7faf5d3c30b7c59a324973833:nostr-user-tag`
and tag elements by the same literal with `:tag`. That pubkey is tapestry's
`LEGACY_Z_TAG_PUBKEY` (`profile-tags/index.js:49`) — **not** any TA key on
either deployment. Tapestry's ADR 0015 records that changing it orphans
historical data across every Brainstorm/Tapestry deployment (the "lost tags"
incident). It is wire-binding and must be copied exactly, with the same
warning comment, and used **only** for `z`-tag composition — never as a signer
or author filter.

This is the story's clearest irreversibility trigger: the literal exists in
more than one repo.

### D9 — One admin endpoint, Observer as a parameter

`POST /admin/trustedLists/{observer_pubkey}` under the existing admin router,
inheriting `verify_token` + `verify_admin_access`. The Observer is a path
parameter, not the caller's session pubkey — the admin acts *on behalf of* a
customer. This differs from tapestry, whose refresh endpoints gate on
`session.pubkey === pin.pubkey` because there the actor and the subject are
the same person.

Tags are processed sequentially, returning one aggregate result. At v1 volumes
(tens of tags) this is adequate and implicitly throttles relay writes.


### D10 — Taggings sync gets its own kind list; `ev_kinds` is not touched

The obvious implementation of D1 is to append kind 39999 to
`nostr_event_transferer.ev_kinds`. **That would be a latent bug.** `ev_kinds`
has a second consumer: `_is_graph_db_populated`
(`app/message_queue_tasks/backfill_redis_relationships.py:28-36`) iterates it
and returns `False` unless *every* listed kind has a **completed**
`nostr_transfer_status` row. Its caller
(`backfill_redis_relationships_if_needed`, line 127) then skips the one-time
Redis relationship backfill with an info log. So on any instance that has not
yet written `DONE_MARKER_KEY`, adding a kind to `ev_kinds` silently disables
the backfill until that new kind's transfer completes — a quiet correctness
regression in an unrelated subsystem, announced only by a log line that reads
like normal startup.

The coupling is meaningful, not accidental: `ev_kinds` denotes *the kinds the
follow/mute/report graph is built from*, and `_is_graph_db_populated` is
reading it with exactly that meaning. Taggings are not a graph-relationship
kind and do not belong in that set.

**Decision:** taggings sync uses a separate module-level list
(`tagging_ev_kinds`) consumed only by the transferer's sync loop. `ev_kinds`
keeps its current five entries and its current meaning.

Rejected: adding 39999 to `ev_kinds` and widening `_is_graph_db_populated` to
filter for graph kinds. It fixes the immediate break but leaves one list
carrying two meanings, so the next kind added re-opens the same trap.

### D11 — Retraction fires only from a trustworthy view

The retraction pass reads back this Observer's live kind-30392 slots (scoped to
their own signing key, filtered on the `tl-tag-` prefix) and empties any slot
not in the current run's set. Two refinements the naive form gets wrong:

**An empty dictionary must NOT return early.** Every tag falling out is the
commonest retraction case; returning early there would leave stale lists
asserting membership forever.

**But emptiness is only actionable when we know why.** Three outcomes produce
zero lists, and they are not equivalent:

| Outcome | View | Retract? |
|---|---|---|
| Nothing ingested (`no_taggings_ingested`) | broken — un-synced relay | **no** |
| No qualifying asserters (`no_qualifying_asserters`) | broken — Observer likely unscored | **no** |
| Taggings + qualifying asserters, no tag met the use threshold | trustworthy | **yes** |

Retracting on either broken view would wipe every live list the Observer has on
the strength of data we already know is missing — the same failure class as
D1-B's silent under-recall, but destructive instead of merely incomplete.

If the read-back scan itself fails, the run retracts nothing and logs: leaving
stale lists in place is strictly safer than guessing.

## Consequences

**Enables**

- A signed, self-describing, per-customer list per used Tag, consumable by any
  nostr client — the input @vitorpamplona needs for search augmentation.
- Taggings become first-class ingested data in this server, which is the
  precondition for the later pin story, for tag-aware search, and for any
  tagging surface in the Brainstorm UI.
- The dictionary and membership aggregations are plain SQL, so raising the
  usage threshold or switching to the sum-of-rank variant (#73 §3) is a query
  change, not an architecture change.

**Constrains / makes harder**

- Two new tables and a migration, plus a new dispatch branch in the hot ingest
  path. The branch is a cheap `z`-tag check for a kind that currently doesn't
  arrive at all, so steady-state cost is bounded by tagging volume — small
  next to kinds 0 and 3.
- Membership is only as complete as ingest. This trades tapestry's
  scan-at-compute-time freshness for recall, deliberately.
- The `z`-tag literal and the kind-30392 tag vocabulary are now a contract with
  tapestry and with downstream consumers. Neither can be changed unilaterally.
- Publishing under per-observer keys means a TL consumer must resolve the
  customer's assistant pubkey first — the same indirection kind-30382 already
  requires, so no new concept, but it does mean `GET /setup/{pubkey}` likely
  owes a 30392 designation row (open question 4).

## Open questions — blocking deploy, not blocking implementation

Corrected 2026-08-25: an earlier revision of this ADR headed this section
"must close before Phase 4". That was wrong, and the distinction matters for
sequencing. None of these four changes the *shape* of the code — the ingest
branch, the two tables, the dictionary and membership computation, the wire
shape, the signing identity, and the endpoint are all fully specified and
testable against synthetic events without any of them answered. What they
determine is whether the feature does anything **on real data once deployed**,
and where its output lands.

So: Phase 4 may proceed under the assumptions recorded per question below.
None may remain open at merge, because each can leave the feature silently
inert in production — the exact failure mode D1's accepted risk and AC15's
visibility handles exist to make loud rather than prevent.

The four questions, and who can answer them:


1. **Do kind-39999 taggings reach us at all?** `NOSTR_TRANSFER_FROM_RELAY` is
   `wss://wot.grapevine.network` (`env.example:18`), a WoT/profile relay.
   Tapestry's taggings live on `dcosl.brainstorm.world` /
   `dcosl.brainstorm.social` behind its opt-in `dcosl` preset. If neither our
   strfry nor our transferer subscribes there, AC1/AC2 have no input and the
   feature is inert on real data. **Owner: @vitorpamplona / David.**
2. **Does the out-of-repo strfry plugin forward kind 39999** onto
   `strfry:events`, or filter by kind? A hard dependency of AC1/AC2.
3. **Where should TLs be published?** Same relay as TAs
   (`NOSTR_UPLOAD_TA_EVENTS_RELAY`), or the `nip85.*` relays tapestry mirrors
   30392–30395 to?
4. **Does `GET /setup/{pubkey}` owe `30392:*` designation rows** alongside its
   five `30382:*` rows (`app/routers/setup/router.py:14-33`)?

**Assumptions Phase 4 proceeds under**, each to be confirmed or corrected
before merge:

- **Q1/Q2** — build the ingest path complete and correct; test it against
  synthetic kind-39999 events. If it turns out no relay we read carries
  taggings, the fix is configuration (a sync source) and not code.
- **Q3** — publish to `NOSTR_UPLOAD_TA_EVENTS_RELAY` (the same relay as this
  observer's TAs) via a dedicated setting that defaults to it, so retargeting
  to the `nip85.*` relays later is an env change, not a diff.
- **Q4** — leave `GET /setup/{pubkey}` untouched. Adding designation rows is
  additive and cheap; adding them speculatively and wrongly is a wire claim we
  would have to retract.

### Empirical finding, 2026-08-26 — Q1 is answered, and the answer is "no"

Measured against the live local relay stack (`infra-strfry-1` on :7777 and
`infra-strfry2-1` on :7778, the hosts `NOSTR_TRANSFER_TO_RELAY` and
`NOSTR_UPLOAD_TA_EVENTS_RELAY` point at):

| Kind | Events held (each relay) |
|---|---|
| 39999 | 76 |
| 39998 | 0 |
| 30382 | 0 |
| 0 | 0 |

The 76 kind-39999 events are **not taggings**. All 76 come from a single
author, carry exactly one tag — `["d", "<uuid>"]` — and hold base64 encrypted
content. No `z` tag of any kind, so no tag-element or tagging concept is
present. Kind 39999 is a general Decentralized-Lists item kind, and this is
some other application's private encrypted data riding it.

Two consequences:

1. **Q1/Q2 are answered for this environment: no taggings arrive here.** The
   feature will be correctly inert on this stack, and reports so via AC15's
   `empty_reason = no_taggings_ingested` rather than looking like a quiet day.
   Wiring a sync source (tapestry's taggings live behind its `dcosl` preset on
   `dcosl.brainstorm.world` / `.social`) remains a deployment question for
   @vitorpamplona / David — it is configuration, not code.
2. **AC3 is load-bearing, not hypothetical.** Foreign kind-39999 traffic
   demonstrably exists on the very relays we read. Running the parser over all
   76 real events yields `0 tag elements, 0 taggings, 76 correctly ignored` —
   the `z`-tag discrimination is what stands between this feature and ingesting
   another app's encrypted blobs as trust assertions.

Q1 and Q3 are also the two flags raised at Gate A and accepted as
resolve-in-flight.

## Blast radius

**Touched:** `app/db_models/__init__.py`, a new Alembic revision, a new repo
module for the two tables (`app/repos/tagging_repo.py`),
`app/repos/user_repo.py` (one new batched query,
`get_qualifying_asserters_for_observer` — the per-observer rank read D3
chose lives with the other Neo4j queries per `app/neo4j_db/CLAUDE.md`'s
"queries live in user_repo" rule; adding a parallel module for one query
would have violated it), `app/message_queue_tasks/process_strfry_event.py`
(one dispatch branch), `app/nostr_event_transferer/nostr_event_transferer.py`
(a new `tagging_ev_kinds` list + its sync call — **not** `ev_kinds`, per D10),
new service modules (`tagging_parse`, `trusted_list_build`,
`trusted_list_service`), a new router under `app/routers/admin/trusted_lists/`,
`app/routers/admin/router.py` (registration), the response schemas —
`app/schemas/trusted_list_schemas.py` (new) registered in
`app/schemas/request_response_schemas.py` per the repo's envelope convention
(every wrapped response subclasses `SuccessfulResponseDataSchema` there),
`app/core/config.py` + `env.example` (**four** settings — the fourth,
`trusted_list_relay`, is the Q3 assumption's env-retargeting knob), and the
directory `CLAUDE.md` files those touch.

**Considered and deliberately left unmodified:**
`app/message_queue_tasks/backfill_redis_relationships.py`. It is the second
consumer of `ev_kinds` (`_is_graph_db_populated`, line 28-36) and would have
been broken by the naive form of D1 — see D10 for the analysis and the
decision that keeps it untouched. Any future change to `ev_kinds` must
re-check this module.

**Explicitly not touched, grep-verified against the current tree:**

- The **search router** and the **cronjobs/scheduler** —
  `grep -rn "process_strfry_event\|ev_kinds\|39999\|30392" app/routers/search/ app/cronjobs/`
  returns no matches, so neither imports the ingest path nor references either
  event kind.
- **Vespa** — `grep -rn "39999\|30392\|tagging" app/core/vespa.py` returns no
  matches; no schema change, no new field, no new upsert, no new caller.
- The **kind-30382 signing/publishing path** and the GrapeRank queue flow: this
  story adds a parallel publisher and does not modify
  `upload_nostr_events.py`.

---

## Amendment 2026-08-27 — D12: weighted member confidence (GrapeRank interpreter)

**Status:** Proposed — pending confirmation from the team conversation in
flight; wire details below may shift before implementation.

### Why

v1 membership is a binary gate plus a head-count: an asserter either clears
`rank >= 3` or doesn't, and past the gate every asserter counts as exactly 1.
Ten rank-3 taggers therefore outweigh two rank-90 taggers — the opposite of
what a web-of-trust should say. Issue #73 §3 anticipated this ("It might also
be measured as the sum of the rank of each Tagging author rather than an
integer count"); the immediate forcing function is search: if the TL carries a
per-member confidence, Vespa's rank profile reads a number instead of
re-deriving trust math at query time.

### Decision

Score each member with **GrapeRank's interpreter formula**
(`specs/graperank.md`, "weight of one data point"), applied single-hop over
the live taggings on one (tag, target) pair. The
input→certainty transformation is the one already in the estate.s code:
`convertInputToConfidence(input, rigor)` in tapestry.s
`src/algos/personalizedGrapeRank/calculateGrapeRank.js:77-83`
(`certainty = 1 − exp(−input × −ln(rigor))`, the closed form of
`1 − ρ^input` — the spec notes the identity). The published score is
`certainty × average`, which **equals raw certainty exactly whenever a member
has no disputes** (average = 1, the common case); a contested member.s score
is discounted by the dispute mass rather than inflated by it — raw certainty
of *total* input would rise with disputes, since a dispute adds weight — not a new formula, the estate's
existing one, so one trust vocabulary covers follows, mutes, reports, and now
taggings.

Mapping, with the spec's symbols:

```
data point = one live tagging (asserter x, polarity p)
    rating r          = +1 when applied (p >= 0.5), −1 when disputed (p <= −0.5);
                        neutral (the reserved open interval) contributes nothing
    edge confidence c = 1.0        (a tagging is a deliberate, targeted act —
                                    unlike a follow, which is ambient; a
                                    taggingConfidence parameter slot exists in
                                    principle but is not introduced)
    attenuation α     = 1.0        (single-hop aggregation; there is no chain
                                    to attenuate)
    weight w          = c × influence(x) × α  =  influence(x)
                        (the asserter's Influence in THIS Observer's WoT —
                         the same `influence_<observer>` the gate reads)

input      = Σ w                over the pair's live taggings
average    = Σ (w × r) / input          (0 when input = 0)
certainty  = 1 − ρ^input                (ρ = rigor)
score      = round( max(average × certainty, 0) × 100 )   — integer 0–100
```

Properties this buys, all inherited from the spec rather than argued fresh:
bounded output; **mass beats count** (ten 0.03-influence appliers yield
`input=0.3 → confidence≈0.19`; two 0.5s yield `input=1.0 → confidence=0.5`);
disputes subtract by weight, and a dispute-dominant pair clamps to 0 rather
than going negative on the wire — the weighted successor of v1's
`applications > disputes`.

**Rigor:** present in the formula, **not yet a knob**. `ρ = 0.5` (the spec
default) as a module constant, published on the event (below) for
reproducibility; promoting it to a setting is a later, cheap change. Per the
operator: we are not tuning rigor yet, only using the formula that carries it.

### What stays

- **The dictionary floor stays at `rank >= 3`, unchanged.** Weighting does not
  replace the gate — it governs confidence *within* a list; the gate snips the
  long tail out of the dictionary entirely. Operator-confirmed rationale:
  ranks 3–5 are a mix of new users and scammers, but **2 and below is almost
  entirely scammers** — a weight-only design would still let a horde of
  rank-1 asserters instantiate the tag itself. The floor is the spam valve;
  the weights are the ranking.
- `TRUSTED_LIST_CUTOFF` keeps its raw-count meaning (≥ cutoff applied
  taggings) as a coarse floor alongside the score.

### Membership predicate (v2)

member iff `applications >= cutoff` **and** `score >= 1` — the second clause
is the weighted successor of `applications > disputes` (net-negative or
negligible-weight pairs round to 0 and drop out). Ordering: score desc, then
pubkey asc (stable republish unchanged).

### Wire

No new shape — tapestry's TL format already reserves the slot. `p` tags gain
the third element: `["p", <pubkey>, "", "<score>"]` (empty relay position,
score as string — the layout tapestry's deployed reader already parses, and
the `includeScoreInTL` branch its ADR 0010 left off by default). The content
JSON gains `"score"` per member alongside the existing
`endorsements`/`disputes` counts. One new metadata tag `["rigor", "0.5"]`
joins `cutoff`/`min-rank` so a consumer can reproduce the number. `metric`
stays `tag-membership` — the score is additive information, not a new metric.

Vespa consumption: the score is an int8 0–100 per (observer, member) — the
same quantum and shape as the existing `quality_scores` tensor, by design.

### Implementation deltas (when confirmed)

- `get_qualifying_asserters_for_observer` returns `{pubkey: influence}`
  instead of a bare list (the query already reads the value).
- `compute_members` becomes the weighted fold above; takes
  `(target, polarity, weight)` triples.
- `build_trusted_list_tags` / content emit the score; ordering flips to
  score desc.
- Story: AC8 (membership predicate + ordering) and AC9 (wire shape) amended;
  new AC for the weighting itself (mass-beats-count, dispute clamp,
  rigor-published). Test plan follows.
- Not touched: the gate (D4), ingest, the dictionary query, signing,
  retraction (D7/D11), the admin surface, AC15 diagnosability.

### Rejected alternative

Plain `Σ influence(applied) − Σ influence(disputed)`: simpler, but unbounded
(needs ad-hoc normalization to publish on a 0–100 quantum), has no
diminishing-returns behavior (2× the sybils buys 2× the score forever,
whereas `1 − ρ^input` saturates), and introduces a second trust formula into
an estate that already standardized one.
