# ADR 0002: Trusted Lists of tagged content

**Status:** Proposed
**Date:** 2026-11-03
**Story:** `engineering-team/stories/trusted-lists/2-trusted-lists-of-tagged-content.md`
**Issue:** [NosFabrica/brainstorm_server#128](https://github.com/NosFabrica/brainstorm_server/issues/128)
**Loom:** E9.2 (build Trusted Lists of tagged content), proposal P-143 (start Tags in search now, and its acceptance test)
**Prerequisite:** [NosFabrica/Brainstorm-UI#334](https://github.com/NosFabrica/Brainstorm-UI/issues/334) (the client tags addressable events by event id instead of coordinate)
**Extends:** ADR `trusted-lists/0001` (Trusted Lists from pubkey Taggings)

## Context

ADR 0001 built one kind-30392 Trusted List per used Tag, listing the **people**
an Observer's web of trust tags with it. Search reads those lists today: the
search relay matches a list by its title and unpacks its members into the
results.

Nothing does the same for tagged **content**. People tag notes, articles,
listings and DList items (books, GitHub Accounts) with the same Tags, and this
server throws those taggings away at parse. So a book tagged "espresso" never
comes back for "espresso books" unless its own text says espresso. That is the
product's main promise failing, not an edge case.

This ADR extends 0001 to every **tagging target**, meaning the kind of thing a
Tag was applied to:

- **people**: the existing 30392 lists, unchanged;
- **an event kind**: notes (1), pictures (20), articles (30023), listings
  (30402), DList headers (39998), …;
- **the items of one DList**: books, GitHub Accounts, restaurants, … A DList
  item's list is the header its own `z` tag names.

Loom's E9 tasks and Tapestry's spawning draft call this a "content category".
This repo says *tagging target*.

### What is on the wire today (checked 2026-11-03)

On `wss://tags.brainstorm.world/relay`, deduplicated across both tagging
namespaces:

| Tagged thing | Taggings | Named by |
|---|---|---|
| Notes (kind 1) | about 112 | event id (`e`) |
| Articles (30023) | 7 | coordinate (`a`) |
| GitHub Accounts items (39999) | 4 | coordinate |
| Book item (39999) | 1 | event id: the client bug, #334 |
| DList header (39998), picture (20) | 1 each | — |
| Target found on no relay | 2 | event id |

That is 128 assertions in all, 19 of them disputes. 117 carry both concept
namespaces (`82b75e47…`, the legacy canonical one, and `a68dbf56…`, the client's
local one) and 11 carry `a68dbf56…` alone. Three of the GitHub Accounts
taggings are "verified human", which is P-77's story ("searching verified
humans shows the profiles and the GitHub accounts that carry the tag").

### The reference implementation

Tapestry `feat/tags`, `src/api/trustedList/refreshPinnedTags.js`:

- `runOneNotePin` publishes a kind-**30393** list of `["e", id]` members at
  `tl-pin-notes-<obs8>-<tagAuthor8>-<slug>`, metric `pinned-tag-notes`.
- `runOneItemPin` publishes a kind-**30394** list of `["a", coordinate]`
  members at `tl-pin-items-<obs8>-<tagAuthor8>-<slug>`, metric
  `pinned-tag-items`. It never publishes an empty one, and it carries **no**
  `a` back-reference to the tag, because on a 30394 an `a` tag *is* a member.
- Both are one list per pinned Tag, across every tagging target, with no
  per-member score.

The membership aggregation (`aggregateNotesTagged`) reads taggings by scanning
the relay at compute time, which ADR 0001 D1 already rejected for this server.

### What the search relay does with these kinds (checked in vespa-eventstore main)

- **Titles are indexed for the whole 30392–30395 family**, by title and nothing
  else (`TrustedListIndexingTest`: "a trusted list is searchable by its title
  and by nothing else").
- **30393 and 30394 unpack** into the events they name, ordered by the 0–100
  score in a member's 4th field (`SearchExpansionTest`: `["e", id, "", "80"]`,
  `["a", coord, "", "90"]`). 30395 and anything newer, including the spawning
  draft's 30396, do not.
- **The signer gate is per kind** (`trust/Delegations.kt`). A list unpacks only
  for a reader whose kind-10040 names its signer on that kind's own row. The
  client's Activate flow writes 30392, 30393 and 30394 rows. The house's 10040
  and Vinney's both name their assistant for all three, on
  `wss://scores.brainstorm.world`.
- **A 30394 unpacks only for a request that also asks for an addressable
  kind.** The unpacked event must match the request's own filters.
- **At most 100 members unpack per matching list** (`SEARCH_EXPAND_MAX_PER_EVENT`).
- **The search relay mirrors 30382–30395** from the relays each 10040 names
  (brainstorm-k8s `charts/vespa-relay/staging-values.yaml`, the provider
  stream), and rebuilds its signer set hourly. Nothing needs copying.

### How the relay matches a title (probed 2026-11-03)

Read-only searches (`kinds:[30392]`, `include:spam`) against the house's 54
people lists on `wss://search.brainstorm.world`:

| Search | Lists found |
|---|---|
| `verified human` | Verified Human |
| `human verified` | Verified Human |
| `verified` | Verified Human |
| `verified humans` | Verified Human |
| `bitcoin physicians` | Bitcoin Physician; Bitcoin Physicians Network |
| `physician network` | Bitcoin Physicians Network |
| `verified podcaster` | *nothing* |
| `grantless applicants` | grantless-applicants |
| `music produc` | Music Producer |
| `podcasts` | Podcast |

**Every searched word must be in the title.** Word order is free; singular and
plural match each other; part-words match; hyphens split words. This one fact
drives D1 and D4.

## Decisions

### D1 — One list per Observer, Tag and tagging target actually used

**Options considered:**

- **A (chosen) — one list per (Observer, Tag, tagging target) pair that a
  qualifying asserter actually used.** "espresso" on books and "espresso" on
  listings are two lists. A pair nobody used gets none. This is the plan the
  team accepted on the 2026-10-29 call (Loom P-112).
- **B — one list per Tag, across every target (Tapestry's shape).** Fewer
  lists, and no need to work out each target. **Rejected:** a list titled only
  "espresso" never matches "espresso books", because the relay needs every word
  in the title ("verified podcaster" finds nothing). That fails the product's
  main search. It also makes every target share one 100-member unpack quota.
- **C — the spawning draft's keys and kinds** (`30394:tag:<k>:<T>`,
  `30396:tag:<D>:<T>`). **Rejected for now:** the draft is early, the relay
  neither mirrors nor unpacks 30396, and nothing reads the draft's `d` form.
  Loom E9.5 records the divergence.

**Consequence accepted:** more lists, one per Observer, Tag and used target.
Building only used pairs bounds the count by real tagging activity.

### D2 — How a tagging target is worked out

| What the tagging names | Tagging target | Member |
|---|---|---|
| `a` = `39999:<pk>:<d>` (a DList item) | the item's DList: the header in the item's own `z` (`39998:…` or `9998` id) | `a` coordinate |
| `a` = `<k>:<pk>:<d>`, any other addressable kind | kind `k` | `a` coordinate |
| `e` = an id whose event is addressable (legacy, #334) | as the two rows above | **its coordinate**, as `a` |
| `e` = an id whose event is not addressable | its kind `k` | `e` id |

- **A coordinate carries its kind**, so listings and articles need no lookup.
- **A DList item's list and an id's kind need the event itself.** They are
  resolved at build time by fetching the target by id or coordinate (D6), and
  cached.
- **An item that claims two DLists** (two header `z` tags) is a member of both
  lists.
- **This rule must match the client's.** Brainstorm-UI's `feat/search-tag-filters`
  branch groups results the same way (`lib/tagTabGroups.ts`, `groupOf`), so the
  lists and the client's chips agree on what a target is.

### D3 — Kinds and members: 30393 for events by id, 30394 for coordinates

- **People:** 30392, unchanged.
- **Non-addressable events:** **30393**, members `["e", <id>, "", "<score>"]`.
- **Addressable events, DList items included:** **30394**, members
  `["a", <coordinate>, "", "<score>"]`.

The score is ADR 0001 D12's 0–100 weighted confidence, on the same
`(target, polarity, weight)` fold. It's in the 4th field so the relay orders
unpacked members by it, as it does for people. Tapestry publishes no score
there; we diverge deliberately.

**Rejected:** 30396 for DList items, per the spawning draft. The relay doesn't
unpack it (Context), so tagged books would never reach search.

### D4 — Title: the Tag's name plus the tagging target's name

```
title = "<tag element name> <tagging target name>"     e.g. "espresso Books", "stoicism Notes"
```

- **A DList's name** is the plural from its header's `names` tag
  (`["names", "GitHub Account", "GitHub Accounts"]` → "GitHub Accounts"),
  falling back to the singular, then to the header's `d`.
- **An event kind's name** comes from a fixed table in `trusted_list_build`:
  1 → Notes, 20 → Pictures, 30023 → Articles, 30402 → Listings, 39998 → Lists.
  A kind with no entry gets no list, and the run reports it (D8), so a missing
  name is seen, not guessed.
- **One form of the name is enough.** The probe shows singular and plural match
  each other.
- `description` carries the Tag element's description, as in 0001 D5.

**Known limit:** a search with a word the title lacks ("espresso books for
beginners") still misses the list. Words beyond the Tag and the target belong
to the query parser (Loom E2), not to list titles.

### D5 — Wire shape

```
kind: 30393 | 30394
d               tl-tag-notes-<observer[0:8]>-<tagAuthor[0:8]>-<slug>-<target>       # 30393
                tl-tag-items-<observer[0:8]>-<tagAuthor[0:8]>-<slug>-<target>       # 30394
title           <tag name> <tagging target name>                                   # D4
description     <tag element description>
metric          tag-membership
observer        <observer pubkey>
source-tag      <tag element coordinate 39999:<author>:<slug>> <tagAuthorPubkey> <slug>
tagging-target  <kind number> | <DList header coordinate, or 9998 header id>
cutoff          <int>
min-rank        <int>
rigor           <float>
e | a           <member> "" <score>                                                 # one per member, best first
content         {"members":[{"id"|"address", "endorsements", "disputes", "score"}]}
```

- **`<target>` in the `d`** is `k<kind>` for an event kind (`k1`, `k30402`), or
  `dl<headerAuthor[0:8]>-<header d>` for a DList.
- **The `tl-tag-` prefix** keeps Tapestry's cleanup, which sweeps `tl-pin-`,
  off these lists, as 0001 D5 does for 30392.
- **No `a` or `p` back-references,** unlike Tapestry's 30393. On a 30394 an `a`
  *is* a member, and keeping the shape the same on both kinds avoids a reader
  that confuses the two. `source-tag` and `tagging-target` carry the identity
  instead, as multi-letter tags no relay indexes.
- **`source-tag` names the Tag by coordinate,** not by event id as 0001 D5's
  30392 does. Event taggings reach their Tag through a tagging header's `a`
  (D6), so the coordinate is all this path has. It is also the identity OPEN.md
  (2026-09-17) wants for every tagging.
- **The 8-character truncation** is inherited from 0001 D5. Loom P-141 proposes
  full-length `d` tags; if it's accepted before this ships, these lists adopt
  it from the start (open question 1).

### D6 — Ingest event taggings and tagging headers; resolve targets at build time

Kind-39999 events already arrive (0001 D10's `tagging_ev_kinds`) and are
dropped by `parse_user_tagging`'s `z` check. Two new parsers in `tagging_parse`,
dispatched from `process_event_kind_39999`:

- **Tagging header:** a `z` to `39998:<ns>:tagging-with-specific-tag` for an
  honored `<ns>`, plus an `a` to its Tag element (`39999:<author>:<slug>`; `e`
  allowed). Stored as its own coordinate → the Tag's coordinate.
- **Event tagging:** a `z` to `39998:<ns>:nostr-event-tag` for an honored
  `<ns>`, a descriptor `z` to a tagging header's coordinate, and exactly one
  target in `a` or `e`. Replaceable on (asserter, `d`), latest-wins on
  `created_at`, as 0001 D2.

**Honored namespaces** become a setting defaulting to both values the client
writes (`82b75e47…` and `a68dbf56…`, Brainstorm-UI
`config/tagging.config.json`). On today's data, 11 event taggings carry only
the second. People taggings keep 0001's legacy-only check: none on the hub
carry the second namespace alone.

A tagging whose header or Tag element hasn't arrived is kept and dropped at
read time, as 0001 does with a missing element.

**Target resolution options considered:**

- **A (chosen) — look targets up at build time, by id or coordinate, and cache
  the answer.** Only tagged events are fetched: 101 distinct ids and 8
  coordinates today. A by-id fetch has none of the recall cap that made 0001 D1
  reject scans. A target that can't be found is skipped and counted (D8), and
  retried next run.
- **B — record every DList item's list as items sync.** **Rejected:** it puts
  every DList item into PostgreSQL to serve the few that get tagged, and it
  still leaves notes tagged by id needing their kind.

Lookups go to `wss://search.brainstorm.world` (an `include:spam` read; it held
99 of the 101 ids) and the tagging's relay hint, through a new
`trusted_list_target_relays` setting.

### D7 — Membership and scoring are 0001's, keyed on the target

Same dictionary rule (pairs used at least `trusted_list_min_tag_uses` times by
a qualifying asserter), same qualifying asserters (0001 D3), same predicate
(0001 D12), same rigor (0001 D13). `compute_members` already folds over a
string key; the key becomes an event id or coordinate instead of a pubkey. An
addressable target tagged by id and by coordinate counts once, under its
coordinate (D2). An asserter who tagged it both ways holds one stance, their
newest tagging (decided at Gate A, 2026-11-03).

### D8 — One run builds all three kinds; retraction covers all three

- **The same admin action** (`POST /admin/trustedLists/{observer}`) builds 30392,
  30393 and 30394 in one run. Each `TagResult` gains `kind` and
  `tagging_target`. The run result gains `unresolved_targets` and
  `unnamed_kinds`, so "nothing published" can be told apart from "couldn't
  work out what was tagged".
- **Retraction** scans the signer's live 30393 and 30394 lists as it does 30392
  (0001 D7, D11). It reads `source-tag`, `tagging-target` and the title off the
  live event instead of parsing the `d`: slugs and DList `d`s both contain `-`,
  so the `d` can't be split reliably.
- **0001's empty-view safeguards apply unchanged** (D11). An untrustworthy
  empty view never reaches retraction.
- **A failed target lookup is an untrustworthy view too.** If the lookup relays
  can't be reached, every content target is unresolved and every content list
  would look empty. In that case the run publishes no 30393 or 30394 lists and
  retracts none. It reports why, and the 30392 lists still build.
- **A list whose members all drop out is republished empty, without the
  retracted marker,** as 0001 does. This diverges from Tapestry's 30394, which
  publishes nothing. An empty list is still a maintained claim, and leaving the
  old members live would keep claiming them.

## Consequences

**Enables**

- "espresso books" returns a book tagged espresso, through the same search
  request and trust model as people. The acceptance test is in Loom P-143:
  "verified human" returns profiles and GitHub Accounts items, and "stoicism"
  returns notes.
- Unbnd's book genres become searchable once they're published as tags (Loom
  K2.7).
- Tagged notes, articles and items can carry "tagged by people you trust" on
  their result cards, which feeds Loom E3 (ranking and provenance).

**Constrains / makes harder**

- More lists per Observer, and a run that grows with tagging activity. Loom
  E9.3 (the schedule) inherits that cost.
- Two new wire shapes (30393 and 30394 from this server) are now a contract
  with the search relay and the client.
- The tagging-target rule (D2) and the title rule (D4) must stay in step with
  the client's grouping rule (`groupOf`).
- A list unpacks only for readers whose 10040 names the assistant on the 30393
  and 30394 rows. A 10040 published before the client added those rows unpacks
  people only, silently.

**Out of scope**

- The client: widening its one search request to 30392–30394 plus the kinds
  they unpack, reading `e`/`a` members, and showing items beside profiles (Loom
  E9.4, Brainstorm-UI). Its tagging fix is Brainstorm-UI#334.
- A schedule (Loom E9.3).
- The spawning draft's kinds and keys (Loom E9.5).

## Open questions

1. **The `d` tag's length.** Keep 0001's 8-character parts, or start these
   kinds full-length if Loom P-141 is accepted first? Nothing reads these kinds
   yet, so this is the cheapest moment to choose.
2. **The kind-name table (D4).** Which kinds get a name at launch, and does a
   kind with no name stay listless (chosen) or get a generic title?
3. **Lookup relays (D6).** Is the search relay plus the tagging's hint enough,
   or should the tag hub be asked too? Two ids resolved nowhere today.
4. **The 100-member unpack cap.** No list is near it. When one is, the cap is a
   relay setting, or a hint that the pair should split further.

## Blast radius

**Touched (expected):** `app/services/tagging_parse.py` (two parsers);
`app/message_queue_tasks/process_strfry_event.py` (the 39999 dispatch);
`app/db_models/__init__.py` plus one Alembic revision (event taggings, tagging
headers, the target cache); `app/repos/tagging_repo.py` (or a sibling for event
taggings); `app/services/trusted_list_build.py` (D3–D5 builders, the kind-name
table); `app/services/trusted_list_service.py` (three kinds, retraction across
kinds); a new target-resolution service (D6);
`app/schemas/trusted_list_schemas.py`; `app/core/config.py` and `env.example`
(honored namespaces, target relays); tests beside 0001's.

**Not touched:** Vespa and the search router; the kind-30382 publish path;
`ev_kinds` (kind 39999 already syncs through `tagging_ev_kinds`); the 30392
build, whose output is byte-identical before and after.
