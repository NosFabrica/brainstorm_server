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
| `a` = `39999:<pk>:<d>` (an addressable DList item) | the item's DList: the header in the item's own `z` | `a` coordinate |
| `e` = the id of a kind-9999 DList item (the regular, non-replaceable item kind) | the item's DList, the same way | `e` id |
| a DList item (39999 or 9999) with no header `z` | its kind (D4 names both "List items") | as above |
| `a` = `<k>:<pk>:<d>`, any other addressable kind | kind `k` | `a` coordinate |
| `e` = an id whose event is addressable (legacy, #334) | as the rows above | **its coordinate**, as `a` |
| `e` = an id whose event is not addressable | its kind `k` | `e` id |

- **39999 and 9999 items are treated the same,** except that one is replaceable
  and so is named by coordinate, and the other by id. A DList holding both
  kinds of item gets two lists for the same Tag: a 30394 and a 30393.
- **A header `z`** is `39998:<pk>:<d>` for an addressable header, or the header's
  event id for a kind-9998 header (DList Header Declaration). The DList is
  identified the same way in `tagging-target` (D5).
- **A coordinate carries its kind**, so listings and articles need no lookup.
- **A DList item's list and an id's kind need the event itself.** They are
  resolved at build time by fetching the target by id or coordinate (D6), and
  cached.
- **An item that claims two DLists** (two header `z` tags) is a member of both
  lists.
- **This rule follows the client's grouping rule,** Brainstorm-UI
  `feat/search-tag-filters` `lib/tagTabGroups.ts` `groupOf`: items of either
  kind group by their header, and anything else, including a headerless item,
  groups by its kind. Two known differences:
  - **An item with two headers** joins both lists here, while `groupOf` files
    it under its first header only (`.find`, and `dlistFields.ts`: "its first
    `z`"). This difference is intended: a book on two DLists should answer a
    search naming either one. The client's chips still show it once.
  - **A 9998 header named by event id:** `groupOf` recognizes a 9998 header
    only in a `9998:<pk>:` coordinate form, though its own `headerReference`
    uses the id. That's a client fix, out of scope here.

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
  1 → Notes, 20 → Pictures, 30023 → Articles, 30402 → Listings, 39998 → Lists,
  and 39999 and 9999 → List items (for an item with no header `z`).
  - **The table is this server's own.** It names what each kind *is*, by its
    NIP (NIP-68 calls kind 20 a picture), in the plain words people search
    with. A title is published data, so it is never taken from a client's
    display labels. Brainstorm-UI shows its own labels ("Photos", "Open
    lists"); that's its display choice and doesn't feed back here.
  - **A kind with no entry gets no list,** and the run reports it (D8), so a
    missing name is seen, not guessed.
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
  `dl<segment>` for a DList, where `<segment>` follows the event-taggings rule
  (Tapestry `protocols/drafts/event-taggings.md`, "The assertion d-tag
  (normative)"), exactly as defined there (Vinney, 2026-11-04):
  - a header named by coordinate (39998) gives `<author8>-<d16>-<hash8>`.
    `hash8` is the first 8 lowercase hex characters of the SHA-256 of the UTF-8
    bytes of the full coordinate, exactly as carried; it is the only part that
    keeps the slot unique. `author8` (the header author's first 8 characters)
    and `d16` (the first 16 UTF-16 code units of the header's `d`, verbatim)
    are readable decoration;
  - a kind-9998 header, named by event id, gives `<id8>`, the id's first 8
    characters.

  Example: `tl-tag-items-<obs8>-<ta8>-espresso-dl<author8>-books-<hash8>`.
  Every `d` then has a bounded length whatever the header's `d` holds
  (colons, spaces, any script), and a tagging of one of these lists stays
  flat under the same rule. Readers never parse the `d`; `tagging-target`
  names the DList in full. **One deliberate difference:** where the 16th code
  unit cuts an emoji in half, JavaScript's `slice` keeps the lone half; no
  event can carry it as UTF-8, so it is dropped here.
  - *Superseded 2026-11-04,* before anything shipped: `dl<author8>-<header d>`,
    with a 9998 header's whole 64-character id in place of its `d`. Unbounded,
    and a hyphenated slug could mimic a DList slot.
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
- **The 8-character truncation** of the observer and the tag author is
  inherited from 0001 D5. Loom P-141 proposes full-length `d` tags; if it's
  accepted before this ships, those two parts adopt it from the start (open
  question 1). The DList segment stays on the event-taggings rule either way.

### D6 — Ingest event taggings and tagging headers; resolve targets at build time

Kind-39999 events already reach `process_event_kind_39999`, and the ones that
aren't tag elements or pubkey taggings are dropped by `parse_user_tagging`'s
`z` check. In production they arrive through neofry (see D10 for the path).
Two new parsers in `tagging_parse`, dispatched from the same function:

- **Tagging header:** a `z` to `39998:<ns>:tagging-with-specific-tag` for an
  honored `<ns>`, plus an `a` to its Tag element (`39999:<author>:<slug>`; `e`
  allowed). Stored as its own coordinate → the Tag's coordinate.
- **Event tagging:** a `z` to `39998:<ns>:nostr-event-tag` for an honored
  `<ns>`, a descriptor `z` to a tagging header's coordinate, and exactly one
  target in `a` or `e`. Replaceable on (asserter, `d`), latest-wins on
  `created_at`, as 0001 D2.

**Honored namespaces.** Which concept namespaces count is a trust default: it
decides whose `nostr-event-tag` and `tagging-with-specific-tag` lists this
server accepts. Options considered:

- **A (chosen) — a setting, `tagging_honored_namespaces`, defaulting to the two
  values the client writes** (`82b75e47…`, the legacy canonical namespace, and
  `a68dbf56…`, Brainstorm-UI `config/tagging.config.json`). A tagging counts if
  it joins any honored namespace.
- **B — legacy only, as 0001 does for people taggings.** **Rejected:** 11 of
  today's 128 event taggings carry only `a68dbf56…`, so they would be dropped
  silently.
- **C — any namespace.** **Rejected:** a namespace is just a pubkey, so anyone
  could seed one and have its taggings counted. The event-taggings spec makes
  the honored set a reader's choice ("Reading: which taggings count is
  reader-determined") precisely so a reader doesn't do this.
- **D — read the client's config at runtime.** **Rejected:** it couples this
  server's trust default to another repo's deployment.

The value now lives in two repos (here and Brainstorm-UI's config), so a
change to the client's namespaces needs the setting changed too. People
taggings keep 0001's legacy-only check: none on the hub carry the second
namespace alone, and changing it is out of scope.

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

**What is cached (Vinney, 2026-11-03):** only answers to lookups **by event
id**, forever: an event id names one immutable event, so its kind, author,
`d` and `z` tags never change. Anything named **by coordinate** (a 39999 item,
a 39998 header) is fetched fresh each run, because a replaceable event can be
republished under another DList or with new `names`. A coordinate of any other
kind still needs no fetch at all. Today that is about 8 coordinate fetches a
run.

### D7 — Membership and scoring are 0001's, keyed on the target

Same dictionary rule (pairs used at least `trusted_list_min_tag_uses` times by
a qualifying asserter), same qualifying asserters (0001 D3), same predicate
(0001 D12), same rigor (0001 D13). `compute_members` already folds over a
string key; the key becomes an event id or coordinate instead of a pubkey. An
addressable target tagged by id and by coordinate counts once, under its
coordinate (D2). An asserter who tagged it both ways holds one stance, their
newest tagging, so the client's switch from ids to coordinates
(Brainstorm-UI#334) never counts anyone twice.

### D8 — One run builds all three kinds; retraction covers all three

- **The same admin action** (`POST /admin/trustedLists/{observer}`) builds 30392,
  30393 and 30394 in one run. Each `TagResult` gains `kind` and
  `tagging_target`. The run result gains `unresolved_targets` and
  `unnamed_kinds`, so "nothing published" can be told apart from "couldn't
  work out what was tagged".
- **Retraction** scans the signer's live 30393 and 30394 lists as it does 30392
  (0001 D7, D11). It reads `source-tag`, `tagging-target` and the title off the
  live event instead of parsing the `d`: slugs and DList `d`s both contain `-`,
  so the `d` can't be split reliably. A live slot missing any of those tags,
  or carrying one it can't read, is left in place and logged: it is never
  retracted under a guessed identity, and the run goes on.
- **A failed read of the live slots retracts nothing,** as 0001's code does
  today (`trusted_list_service`, the slot scan's `except`). The scan covers
  all three kinds, so a failure stops retraction for all three; publishing
  goes ahead.
- **0001's empty-view safeguards apply unchanged** (D11). An untrustworthy
  empty view never reaches retraction.
- **A failed target lookup is an untrustworthy view too.** If the lookup relays
  can't be reached, every content target is unresolved and every content list
  would look empty. In that case the run publishes no 30393 or 30394 lists and
  retracts none. It reports why, and the 30392 lists still build.
  - **"Can't be reached" means any configured lookup relay**
    (`trusted_list_target_relays`) failing to answer, not all of them. The
    search relay holds nearly every tagged event (99 of 101 ids), so with it
    down and only a tagging's hint relay answering, most targets would come
    back unresolved and their lists would be republished emptied (below)
    without tripping this guard. Hint relays are best effort: one failing
    leaves the view trusted.
- **A list whose members all drop out is republished empty, without the
  retracted marker,** as 0001 does. This diverges from Tapestry's 30394, which
  publishes nothing. An empty list is still a maintained claim, and leaving the
  old members live would keep claiming them.

### D9 — The setup endpoint advertises 30393 and 30394 rows

`GET /setup/{pubkey}` returns the rows an Observer's kind-10040 should carry.
They come from `app/services/duty_menu_build.py` `designation_rows`, the one list
of the assistant's duties. The same list, through `offer_rows`, is what the
assistant's signed Duty Menu offers and what the Treasure Map check
(`self_map_build`) reads. Today it has one bare Trusted List row, `"30392"`.
The search relay's signer gate is per kind (Context), so a 10040 that names the
assistant only on `30392` unpacks people lists and never content lists.

**Options considered:**

- **A (chosen) — add bare `"30393"` and `"30394"` rows to `designation_rows`**,
  with the same assistant key and relay as the `"30392"` row. The server then
  advertises exactly what it publishes. Everything that reads the list sees
  the change:
  - **`/setup`** gains the two rows, straight after `"30392"` and before the
    Dictionary rows (`"39998"`, `"39999"`), so the three Trusted List rows sit
    together. `tests/test_setup_designation.py`'s check that the Dictionary
    rows come right after `"30392"` becomes "right after `"30394"`".
  - **The Duty Menu offers two more duties.** A customer's menu is signed only
    when they ask (`duty_menu_service`), and the UI shows a menu that differs
    from the offer as out of date. So every customer's existing menu will show
    as out of date until they republish it. That's accurate: the assistant now
    offers more.
  - **The Treasure Map check** (`self_map_build.evaluate_self_map`, served by
    the check endpoint as `duties` and `should_say`) changes in one place.
    Trusted Lists are never live (`evaluate_live` answers `not-scheduled` for
    every kind 30390–30399), so the new rows add nothing to `should_say`: no
    self-entries. But `duty_lines` gives one line per offered row a reader's
    key wins, so a reader whose 10040 carries `/setup`'s rows gets two more
    duty lines, `30393` and `30394`, both `not-scheduled`, exactly like
    `30392`'s. No code changes there; the module docstring's list of offered
    keys gains the two.
  - **Brainstorm-UI:** `services/trustLists.ts` `designationFor` takes its
    Trusted List key and relay from the `"30392"` row only, so it's
    unaffected. `lib/treasureMap.ts` `offeredDuties` turns every `/setup` row
    into an offered duty in My Assistants (`hooks/useMyAssistants.ts`), so the
    two rows appear there, correctly.
- **B — leave `/setup` alone, because Brainstorm-UI already writes all three rows
  from the `"30392"` one** (`lib/nip85Declaration.ts`, `LIST_KINDS`).
  **Rejected:** that is a client convention, not something this server says. A
  client that copies `/setup`'s rows into a 10040 verbatim would name 30392
  only, and its content lists would silently never unpack.

Bare rows, not `30393:<name>`, for the reason `duty_menu_build`'s own comment
gives for `"30392"`: Tapestry's reader treats a named 3039x row as a reserved
override and ignores it.

### D10 — Replay the taggings already received, once, after deploy

**How taggings reach this server in production** (brainstorm-k8s
`charts/brainstorm/prod-values.yaml`, `neofry` section; staging is the same):

1. **Clients publish taggings to the tag hub,** `wss://dcosl.brainstorm.world`
   (Brainstorm-UI `config/tagging.config.json` `tagRelays`). The Tapestry
   instances, `tags.brainstorm.world` among them, sync through it.
2. **neofry's router has a `dcosl` stream** pulling kind 39999 down from that
   hub, live, into neofry's store.
3. **neofry forwards kind 39999** (it's in `redisKinds`) onto the
   `strfry:events` queue this server consumes, which dispatches it to
   `process_event_kind_39999`.
4. **The back-catalogue was filled once** by a manual `strfry sync` from the hub,
   recorded in the same values file.

The transferer's `tagging_ev_kinds` (0001 D10) is a separate one-time backfill.
It stops for good once marked complete, and the recurring transferer covers
`ev_kinds` only.

**So every note and item tagging received before this ships was dropped at
parse,** and nothing will deliver it again. The acceptance test's data (the
"verified human" GitHub Accounts taggings) is in that set.

**Options considered:**

- **A (chosen) — a one-off replay script, `scripts/backfill_event_taggings.py`.**
  It reads kind-39999 events from `strfry scan` on stdin, runs each through
  `process_event_kind_39999`, and is dry-run by default (`--apply` to write).
  This is the pattern `scripts/backfill_designations.py` set for kind 10040:

  ```
  kubectl -n <ns> exec brainstorm-neofry-0 -c neofry -- \
      /app/strfry scan '{"kinds":[39999]}' \
    | kubectl -n <ns> exec -i deploy/brainstorm-server -- \
      poetry run python -m scripts.backfill_event_taggings --scan - [--apply]
  ```

  Upserts are latest-wins (D6), so a rerun is safe, and so is a tagging that
  arrives live during the replay. Run it after the release with the new
  parsers is serving, so nothing lands between the replay and live ingest.
- **B — reset the transferer's 39999 row so its backfill re-runs.**
  **Rejected:** it re-sends events to a relay that already holds them. neofry
  forwards an event to the queue when it stores it, so events it already holds
  are not forwarded again. The same values file records this for kind 10040:
  "10040s already in LMDB before that restart never reach redis".
- **C — re-sync from the hub** (another `strfry sync`). **Rejected** for the same
  reason: a sync only fetches what neofry is missing.

**Sources.** The tag hub and `tags.brainstorm.world` held the same
tagging-related events on 2026-11-03 (1,368 each, one different on each side).
The search relay held 8 more, published to other relays. It serves an
anonymous read that carries `"search": "include:spam"`, but neofry's router
can't send one: strfry's filter parser rejects any key it doesn't know
(`src/filters.h`, "unrecognised filter item"), `search` included. So the search
relay can't be a router stream without a strfry change or NIP-42 sign-in. A
one-off read can reach it, though. The replay can take a second input, a
`nak req -k 39999 --search include:spam` dump from the search relay, through the
same script, since upserts are latest-wins and duplicates are harmless.
Adding live sources is an ops question, not part of this story.

**Owners to confirm before running:** the replay runs in production pods, so
the cluster operators (Jeremy, Enes, and possibly Vitor) confirm the command
and the timing.

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
- The tagging-target rule (D2) must stay in step with the client's `groupOf`,
  apart from the differences D2 records. It's the protocol's rule (an item's
  DList is the header its `z` names), so both sides follow the protocol rather
  than each other. The kind names (D4) are this server's alone.
- A list unpacks only for readers whose 10040 names the assistant on the 30393
  and 30394 rows. A 10040 published before those rows existed (in the client's
  Activate flow, and in `/setup` per D9) unpacks people only, silently, until
  it is republished.
- The honored namespaces (D6) are a value held in two repos.

**Out of scope**

- The client: widening its one search request to 30392–30394 plus the kinds
  they unpack, reading `e`/`a` members, and showing items beside profiles (Loom
  E9.4, Brainstorm-UI). Its tagging fix is Brainstorm-UI#334.
- The admin "View list" in Brainstorm-UI's run-result card, which reads only
  kind 30392 (`components/admin/trusted-lists/listMembers.ts`). It needs to
  read each result's `kind`: Brainstorm-UI#335. Until then, "View list" on a
  30393 or 30394 result finds nothing.
- `groupOf`'s 9998-header-by-id gap (D2).
- A schedule (Loom E9.3).
- The spawning draft's kinds and keys (Loom E9.5).

## Open questions

1. **The `d` tag's length.** Keep 0001's 8-character observer and tag-author
   parts, or start these kinds full-length if Loom P-141 is accepted first?
   Nothing reads these kinds yet, so this is the cheapest moment to choose.
   (The DList part is settled in D5.)
2. **The kind-name table (D4).** Which kinds get a name at launch, and does a
   kind with no name stay listless (chosen) or get a generic title?
3. **Lookup relays (D6).** Is the search relay plus the tagging's hint enough,
   or should the tag hub be asked too? Two ids resolved nowhere today.
4. **The 100-member unpack cap.** No list is near it. When one is, the cap is a
   relay setting, or a hint that the pair should split further.

## Blast radius

**Touched (expected):**

- `app/services/tagging_parse.py`: two parsers (D6).
- `app/message_queue_tasks/process_strfry_event.py`: the 39999 dispatch.
- `app/db_models/__init__.py` plus one Alembic revision: event taggings,
  tagging headers and the target cache.
- `app/repos/event_tagging_repo.py` (new): those three tables. `tagging_repo.py`
  keeps tag elements and pubkey taggings.
- `app/services/tagging_target_service.py` (new): target lookup and caching
  (D2, D6).
- `app/services/trusted_list_build.py`: the D3–D5 builders and the kind-name
  table.
- `app/services/trusted_list_service.py`: three kinds, retraction across kinds,
  and the failed-lookup guard (D8).
- `app/routers/admin/trusted_lists/router.py`: copies the new result fields
  (`kind`, `tagging_target`, `unresolved_targets`, `unnamed_kinds`) into the
  response, and updates its description, which still says 30392 only.
- `app/schemas/trusted_list_schemas.py`: those fields.
- `app/services/duty_menu_build.py`: the `"30393"` and `"30394"` rows in
  `designation_rows`, after `"30392"` (D9). `app/routers/setup/router.py` and
  `duty_menu_service` only call it.
- `app/services/self_map_build.py`: the module docstring's list of offered
  keys only (D9). Its logic already treats every 3039x kind as a Trusted List.
- `app/core/config.py` and `env.example`: `tagging_honored_namespaces` and
  `trusted_list_target_relays`.
- `scripts/backfill_event_taggings.py` (new): the D10 replay.
- `BIBLE.md` §5: how taggings reach this server (D10). `BIBLE.md` §3's
  account of the setup endpoint's rows and `app/routers/CLAUDE.md`'s
  `setup/router.py` entry both list the rows exactly, and gain `30393` and
  `30394` (D9).
- `scripts/test_admin_trusted_lists.py`: the admin endpoint's smoke script.
  Its `--verify-relay` read goes from kind 30392 to 30392–30394, and `--seed`
  gains an event tagging beside its pubkey tagging, so it exercises what the
  endpoint now builds.
- Tests beside 0001's, plus the three that pin the offered rows' exact order:
  `tests/test_setup_designation.py` (Dictionary rows now follow `"30394"`),
  `tests/test_assistant_duty_menu.py` (`EXPECTED_KEYS` gains `"30393"`,
  `"30394"`), and `tests/test_assistant_treasure_map.py` (the duty-line keys
  built from `designation_rows`, at its U14 test and the check endpoint's,
  gain the two `not-scheduled` lines).

**Brainstorm-UI consumers of this server's output** (not changed by this
story): the admin run-result card (Brainstorm-UI#335), My Assistants'
offered duties (D9), and search (Loom E9.4).

**Not touched:** the search-relay client (`app/core/search_relay.py`) and the
Open Ranking routes (`app/routers/open_ranking/`); the kind-30382 publish path;
`ev_kinds` (kind 39999 already syncs through `tagging_ev_kinds`); the 30392
build, whose output is byte-identical before and after.
