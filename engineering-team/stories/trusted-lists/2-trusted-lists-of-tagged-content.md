# Story 2: Trusted Lists of tagged content

**Status:** In progress
**Created:** 2026-11-03
**Type:** Feature
**Source spec:** [NosFabrica/brainstorm_server#128](https://github.com/NosFabrica/brainstorm_server/issues/128)
**Design:** ADR [`trusted-lists/0002`](../../decisions/trusted-lists/0002-trusted-lists-of-tagged-content.md), extending ADR [`trusted-lists/0001`](../../decisions/trusted-lists/0001-trusted-lists-from-taggings.md)
**Loom:** E9.2 (build Trusted Lists of tagged content); P-143 (start Tags in search now, and its acceptance test)
**Classification:** ADR (irreversibility triggers: wire format / event shape,
schema + migration, cross-repo contract, auth/trust default (the honored
namespaces, ADR 0002 D6; the membership predicate, which drops the count rule
for 30392 too, D7), a value held in more than one repo (the namespaces
and the tagging-target rule, kept in step with Brainstorm-UI; the DList slot
segment, Tapestry's event-taggings rule, ADR 0002 D5)). The
story runs the **Standard**
phases per [`workflows/light-profile.md`](../../workflows/light-profile.md)
§"Gate A".

## Background

Story 1 (ADR 0001) built one kind-30392 Trusted List per used Tag, listing the
**people** an Observer's web of trust tags with it. The search relay matches
those lists by title and returns their members, which is how "podcaster" finds
podcasters.

People also tag notes, articles, listings and DList items (books, GitHub
Accounts) with the same Tags. This server receives those taggings (kind 39999
reaches it through neofry, BIBLE §5) and drops them at parse. So a book tagged
"espresso" never comes back for "espresso books" unless its own text says
espresso.

This story builds the same lists for tagged content: one per Observer, Tag and
tagging target used, as kind 30393 (events by id) and 30394 (events by
coordinate). The search relay already mirrors, indexes and unpacks both kinds,
so no relay change is needed (ADR 0002, Context).

## User-facing description

As someone searching Brainstorm, when I search "espresso books" I want to see
the books my trusted people tagged espresso, even when a book's own text never
says espresso.

As a Brainstorm admin, I want the same "Publish trusted lists" action that
builds a customer's people lists to build their lists of tagged content too.

## Vocabulary

Per [`CONTEXT.md`](../../../CONTEXT.md), story 1's vocabulary, and tapestry's
`protocols/drafts/event-taggings.md`:

- **Event tagging**: a kind-39999 assertion that an event carries a Tag. Its
  target is in `a` (an addressable event's coordinate) or `e` (an event id). It
  reaches its Tag indirectly, through a `z` to a tagging header.
- **Tagging header**: a kind-39999 event, one per Tag, at
  `39999:<author>:tagging:<slug>-tagging`, on the `tagging-with-specific-tag`
  list. Its `a` names the Tag element it applies.
- **Tagging target**: the kind of thing a Tag was applied to: people, one event
  kind, or the items of one DList. A DList item's DList is the header its own
  `z` names. Not the Observee.
- **Pair**: one (Tag, tagging target) combination. One Trusted List per
  Observer per used pair.
- **Honored namespaces**: the concept pubkeys whose `nostr-event-tag` and
  `tagging-with-specific-tag` lists this server accepts. Defaults to the two
  the client writes: `82b75e47…` (legacy canonical) and `a68dbf56…`.

## Acceptance criteria

Testable from the outside. Each criterion gets at least one test handle in the
test plan.

- [ ] **AC1: tagging header ingest.** A kind-39999 event with a `z` to
      `39998:<ns>:tagging-with-specific-tag` for an honored `<ns>`, and an `a`
      (or `e`) naming a Tag element, is persisted as its own coordinate mapped
      to the Tag's coordinate. A newer event at the same coordinate replaces it;
      an older one is ignored.
- [ ] **AC2: event tagging ingest.** A kind-39999 event with a `z` to
      `39998:<ns>:nostr-event-tag` for an honored `<ns>`, a descriptor `z` to a
      tagging header coordinate, and a target in `a` or `e` is persisted with
      its event id, asserter, `d`, target value and slot (`a`/`e`), descriptor
      coordinate, polarity and `created_at`. It's replaceable on (asserter,
      `d`): newer wins, older is ignored, including a flip between apply and
      dispute.
- [ ] **AC3: both namespaces honored.** A tagging or header carrying only the
      `a68dbf56…` namespace is ingested, as is one carrying only `82b75e47…` or
      both. One carrying neither is not. The honored set is a setting.
- [ ] **AC4: malformed and foreign events ignored.** An event tagging with no
      target, no descriptor `z`, or a target that isn't a 64-hex id or a
      `<kind>:<64-hex>:<d>` coordinate is skipped without raising, and the
      consumer continues the batch.
- [ ] **AC5: story 1's ingest unchanged.** Tag elements and pubkey taggings
      ingest exactly as before. Story 1's AC1–AC4 still pass.
- [ ] **AC6: tagging target resolution.** Per ADR 0002 D2:
      - an `a` coordinate of kind 39999 resolves to the DList its item's own
        `z` names (a `39998` coordinate or a `9998` id);
      - an `e` id of a kind-9999 item resolves to its DList the same way, and
        the member is the id (a 30393);
      - a 39999 or 9999 item with no header `z` resolves to its kind, named
        "List items";
      - an `a` coordinate of any other kind resolves to that kind, with no
        lookup;
      - an `e` id whose event is not addressable resolves to its kind, and the
        member is the id;
      - an `e` id whose event is addressable resolves as a coordinate would,
        and **the member is its coordinate**;
      - an item claiming two DLists is a member of both lists.
      Answers to lookups by event id are cached across runs; anything named
      by coordinate is fetched fresh each run (ADR 0002 D6).
- [ ] **AC7: unresolved targets are skipped, counted and retried.** A target
      that no lookup relay returns joins no list.
      It's counted in the run result's `unresolved_targets` and looked up again
      next run.
- [ ] **AC8: a failed lookup retracts nothing.** If the lookup relays can't be
      reached, the run publishes no 30393 or 30394 lists and retracts none. It
      says why in the run result. The 30392 lists still build and publish.
      Any configured lookup relay failing counts; a tagging's hint relay
      failing does not (ADR 0002 D8).
- [ ] **AC9: pairs dictionary.** For a given Observer, the content dictionary
      holds exactly those (Tag, tagging target) pairs with at least
      `trusted_list_min_tag_uses` non-neutral taggings by asserters who qualify
      in **that Observer's** web of trust (story 1's AC5–AC6 rule). Taggings
      whose header or Tag element hasn't been ingested are dropped at read
      time, not rejected at write time.
- [ ] **AC10: membership and score.** Members are decided and scored exactly as
      story 1's AC7–AC8c (polarity buckets, `applications >= cutoff`,
      weighted score `>= 1`, the Observer's rigor), keyed on the target, with
      **no count rule** (`applications > disputes` is dropped for all three
      kinds, ADR 0002 D7, Vinney 2026-11-04: one apply at Influence 0.9
      against three disputes at 0.05 is listed, scoring 37). An addressable event tagged once by id and once by
      coordinate by different asserters counts as one target with both
      taggings. **One stance per asserter per target:** an asserter who tagged
      the same target by id and by coordinate counts once, their newest
      tagging winning. Members are ordered by score descending, then key
      ascending.
- [ ] **AC11: wire shape.** Each list matches ADR 0002 D5:
      - kind 30393 with `["e", id, "", "<score>"]` members, or kind 30394 with
        `["a", coordinate, "", "<score>"]` members;
      - `d` = `tl-tag-notes-…` / `tl-tag-items-<observer8>-<tagAuthor8>-<slug>-<target>`,
        where a DList's `<target>` is `dl` plus the event-taggings rule's
        segment (`<author8>-<d16>-<hash8>` by coordinate, `<id8>` by event id),
        byte-for-byte as Tapestry's draft defines it, but for the one case
        D5 records (a character cut in half at the 16th code unit);
      - the tags `title`, `description`, `metric` = `tag-membership`,
        `observer`, `source-tag` (Tag coordinate, author, slug),
        `tagging-target`, `cutoff`, `min-rank` and `rigor`;
      - `content` `{"members":[{"id"|"address","endorsements","disputes","score"}]}`;
      - **no** `a` or `p` tag other than members.
- [ ] **AC12: title.** The title is `<Tag name> <tagging target name>`. A
      DList's name is the plural in its header's `names` tag, else the
      singular, else the header's `d`. An event kind's name comes from the
      server's own fixed table, never a client's display labels (1 Notes, 20
      Pictures, 30023 Articles, 30402 Listings, 39998 Lists, 39999 and 9999
      List items). A pair whose kind has no name gets no
      list and appears in the run result's `unnamed_kinds`.
- [ ] **AC13: signing and relay.** Lists are signed by the Observer's assistant
      nsec and published to the same relay as their 30392 lists.
- [ ] **AC14: one run, three kinds.** `POST /admin/trustedLists/{observer}`
      builds 30392, 30393 and 30394 in one run. Each per-list result carries
      `kind` and `tagging_target`. The run result carries `unresolved_targets`
      and `unnamed_kinds`.
- [ ] **AC14b: setup advertises the new kinds.** `GET /setup/{pubkey}` returns
      bare `"30393"` and `"30394"` rows straight after `"30392"` and before
      the Dictionary rows, with the same assistant key and relay (ADR 0002 D9).
      The existing rows are unchanged. The Duty Menu offer carries the same two
      rows, and the Treasure Map check of a Map built from `/setup`'s rows
      gives them `not-scheduled` duty lines and no self-entries.
- [ ] **AC15: retraction across kinds.** A 30393 or 30394 slot this signer
      published whose pair has left the dictionary is replaced by an empty list
      carrying `["status","retracted"]`. Its identity tags are read off the
      live event, not parsed from its `d`. A pair whose publish failed this run
      keeps its slot (story 1's AC14), and an untrustworthy empty view retracts
      nothing (ADR 0001 D11). A live slot missing its identity tags is left in
      place and logged, and a failed read of the live slots retracts nothing
      of any kind (ADR 0002 D8).
- [ ] **AC16: emptied list republished.** A pair still in the dictionary whose
      members all fall out is republished with no members and **no** retracted
      marker.
- [ ] **AC17: people lists unchanged but for the count rule.** For the same
      input, the 30392 events a run produces are identical to story 1's
      output, except for targets whose membership the count rule decided
      (ADR 0002 D7): those with more disputes than applies by count but a
      score of at least 1 are now members.
- [ ] **AC17b: replay script.** `scripts/backfill_event_taggings.py --scan -`
      reads `strfry scan` output (one event per line) and runs each kind-39999
      event through `process_event_kind_39999`. Without `--apply` it writes
      nothing and prints counts (headers, event taggings, tag elements, pubkey
      taggings, ignored). With `--apply` it writes, and a second run changes
      nothing (ADR 0002 D10).
- [ ] **AC18: live acceptance (after deploy and the replay, not CI).** Through
      the house's perspective on `wss://search.brainstorm.world`:
      - "verified human" with kinds `[0, 39999, 30392, 30394]` returns the
        profiles and the GitHub Accounts items carrying the tag;
      - "stoicism" with kinds `[1, 30393]` returns notes tagged stoicism;
      - a search naming a Tag and a tagging target ("espresso books") returns
        that target's items carrying the Tag, once such a tagging exists.

## Edge cases (not derivable from one AC)

- **A tagging arrives before its header, or its header before its Tag element.**
  Kept at write; joins once both exist.
- **A tagging header is republished with a different `a`** (repointed at
  another Tag). Latest wins. Its taggings follow the new Tag on the next run.
- **An id-tagged addressable event has a newer version.** The lookup by id
  returns the old version, whose coordinate is still the right one.
- **A target event was deleted** (NIP-09) or exists on no lookup relay:
  unresolved (AC7), never a crash.
- **Two Tags share a name but have different authors.** Two lists with the same
  title, distinct `d`s. That's already true of 30392.
- **A DList's `d` is long.** The list's `d` doesn't grow with it: the DList
  segment is bounded by the event-taggings rule (ADR 0002 D5).
- **A Tag element is itself the tagging target** (a tagged tag). Its DList is
  the Tag concept header, named from that header's `names`, like any DList.
- **One asserter tags the same target by id and by coordinate.** Two taggings
  with different `d`s and the same subject. Counted once, newest winning
  (AC10).
- **A pair with more than 100 members.** It publishes whole; the relay unpacks
  the first 100 (ADR 0002, Context).

## Out of scope

- **The client**: widening Brainstorm-UI's one search request to 30392–30394
  plus the kinds they unpack, reading `e`/`a` members, and showing items beside
  profiles (Loom E9.4).
- **Tagging addressable events by coordinate in the client:**
  NosFabrica/Brainstorm-UI#334, a prerequisite for listings tagged after this
  ships.
- **The admin "View list"** for 30393 and 30394 results in Brainstorm-UI:
  NosFabrica/Brainstorm-UI#335.
- **Running the replay in production:** the cluster operators (Jeremy, Enes,
  and possibly Vitor) confirm the command and timing. This story ships the
  script and the runbook (ADR 0002 D10, BIBLE §5).
- **A schedule** (Loom E9.3). This story keeps the admin action.
- **The spawning draft's kinds and keys**, including 30396 (Loom E9.5).
- NIP-73 external ids (30395).

## Gate-command exception (approved)

`poe check_all` is red on `main` before this story's diff: six tests fail,
filed as [NosFabrica/brainstorm_server#129](https://github.com/NosFabrica/brainstorm_server/issues/129).
Four billing reconcile tests seed a subscription with no plan into a column
that no longer allows it; one billing plan-write test passes an argument the
function no longer takes; one passkey-vault test gets a 404 and fails alone
too. None touches Trusted Lists or taggings. Baseline at `90372a9`, test
database at alembic head: format, imports, lint and types pass; the suite is
1,770 passed, 6 failed.

**Operator decision (Vinney, 2026-11-03): until #129 is fixed, this story's
gate is `check_all` with exactly these six tests deselected.** Every other
stage and test must pass. If #129 lands first, the plain `poe check_all` is
the gate. J3 runs:

```
poetry run poe check_fmt && poetry run poe check_imports && \
  poetry run poe check_lint && poetry run poe check_types && \
poetry run pytest ./tests \
  --deselect tests/integration/test_billing_reconcile_candidates_integration.py::test_the_error_clock_runs_from_the_first_failure_of_its_kind \
  --deselect tests/integration/test_billing_reconcile_candidates_integration.py::test_an_abandoned_checkout_drops_out_and_everything_else_stays \
  --deselect tests/integration/test_billing_reconcile_candidates_integration.py::test_an_abandoned_checkout_leaves_the_alarming_sections_for_its_own \
  --deselect tests/integration/test_billing_reconcile_candidates_integration.py::test_a_settled_subscription_stops_being_read_and_the_unsettled_stay \
  --deselect tests/integration/test_billing_plan_write_integration.py::test_a_plan_write_returns_an_object_whose_timestamps_can_be_read \
  --deselect tests/test_passkey_vault.py::test_a_new_device_opens_the_vault_with_the_passkey
```

## Implementation scope notes (J3 item 2)

Outside the ADR's declared blast radius, each needed by what is inside it:

- **`tests/integration/tagging_harness.py`:** cleans the three new tables
  beside story 1's two (`clean_tagging_tables`), so the integration tests
  start and end empty.
- **The directory guides** (`app/db_models/`, `app/repos/`, `app/services/`,
  `app/message_queue_tasks/`, `scripts/` `CLAUDE.md`): each describes the
  module it sits beside, and the new tables, repo, services, dispatch and
  script are described there. Docs only.
- **`CONTEXT.md`:** gains the glossary entry "Tagging target" (what a list is
  a list *of*: people, an event kind, or one DList), the term this story, its
  ADR and the code use (`TaggingTarget`, the `tagging-target` tag). The
  glossary is where a new domain term is defined before it's used. Docs only.
- **`alembic/versions/e7b3c9d1f2a8_add_event_tagging_tables.py`:** the "one
  Alembic revision" the blast radius names. `alembic check` reports only two
  drifts older than this branch (dropped Vespa-era columns), none from these
  tables.
- **The shared membership predicate** (`compute_members` in
  `app/services/trusted_list_build.py`): the count rule is dropped for all
  three kinds, people lists included (operator decision, Vinney, 2026-11-04;
  ADR 0002 D7). So 30392 output changes for targets the count rule decided,
  and only those (AC17); story 1's test vectors G and N in
  `tests/test_trusted_list_membership.py` flip to members.

## Open questions

From ADR 0002, plus one from this story:

1. **The `d` tag's length:** 8-character parts as in 0001, or full-length if
   Loom P-141 is accepted first?
2. **The kind-name table:** which kinds get names at launch?
3. **Lookup relays:** the search relay and the tagging's hint, or the tag hub
   too?
4. **The 100-member unpack cap:** revisit when a list nears it.
5. ~~**One asserter, one target, two taggings**~~ (by id and by coordinate,
   before and after #334 ships). **Resolved at Gate A (Vinney, 2026-11-03):**
   one stance per asserter per target, newest winning (AC10).
