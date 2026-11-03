# Test Plan: Story 2 — Trusted Lists of tagged content

**Story:** `engineering-team/stories/trusted-lists/2-trusted-lists-of-tagged-content.md`
**ADR:** `engineering-team/decisions/trusted-lists/0002-trusted-lists-of-tagged-content.md`
**Date:** 2026-11-03

## Coverage map

Every AC has at least one handle. `U*` = fast suite (pure functions, or the
service with its boundaries patched; no backend). `I*` = integration suite
(real Postgres, Neo4j and the two isolated strfry relays). `S*` = regression
sentinels, listed under Edge cases. `M1` = the live acceptance check, run by
hand after deploy.

| Criterion | Handle | Test name | File | Level |
|---|---|---|---|---|
| AC1 tagging header parsed, `a` and `e` forms | U1 | `test_tagging_header_parsed_from_event[a, e]` | `tests/test_event_tagging_ingest.py` | fast |
| AC1 newer header replaces, older ignored | I1 | `test_tagging_header_upsert_newer_replaces_older_ignored` | `tests/integration/test_event_tagging_store_integration.py` | integration |
| AC2 event tagging parsed, every field, `a` and `e` targets | U2 | `test_event_tagging_parsed_from_event[a, e]` | `tests/test_event_tagging_ingest.py` | fast |
| AC2 replaceable on (asserter, `d`), incl. apply→dispute flip, older ignored | I2 | `test_event_tagging_upsert_latest_wins_across_polarity_flip`, `test_event_tagging_older_event_does_not_overwrite` | `tests/integration/test_event_tagging_store_integration.py` | integration |
| AC3 namespaces: legacy only, client only, both ingested; neither not | U3 | `test_honored_namespaces[legacy, client, both, neither]` (for headers and taggings) | `tests/test_event_tagging_ingest.py` | fast |
| AC3 the honored set is the setting | U3b | `test_honored_set_comes_from_the_setting` (set to the legacy namespace only; a client-only tagging is then dropped) | `tests/test_event_tagging_ingest.py` | fast |
| AC4 malformed event taggings return nothing | U4 | `test_malformed_event_tagging_returns_none[…]` (no target; both `a` and `e`; no descriptor `z`; descriptor not a header coordinate; target id not 64-hex; target coordinate malformed) | `tests/test_event_tagging_ingest.py` | fast |
| AC4 consumer continues the batch | U4b | `test_malformed_event_tagging_does_not_stop_the_batch` (one bad event between two good ones through `process_event_kind_39999`; both good ones are written) | `tests/test_event_tagging_ingest.py` | fast |
| AC5 story 1's ingest unchanged | U5 + existing | `test_pubkey_tagging_and_tag_element_still_route_to_story_1_writers` (dispatch writes them to story 1's tables, never the new ones); story 1's `tests/test_tagging_ingest.py` and `tests/integration/test_tagging_store_integration.py` unedited and green | `tests/test_event_tagging_ingest.py` | fast + integration |
| AC6 tagging target, every row of D2's table | U6 | `test_tagging_target_of[…]` (39999 by `a` with a `39998` header `z`; 39999 by `a` with a 9998 header id `z`; 9999 by `e`; headerless 39999 and 9999 → kind; 30402 by `a`, no fetch; kind-1 note by `e`; addressable event by `e` → its coordinate; a Tag element as target → its concept header's DList) | `tests/test_tagging_target.py` | fast |
| AC6 addressable event tagged by `e`: the member is its coordinate | U6b | `test_id_tagged_addressable_member_is_its_coordinate` | `tests/test_tagging_target.py` | fast |
| AC6 item claiming two DLists is in both lists | U6c | `test_two_header_item_joins_both_lists` | `tests/test_tagging_target.py` | fast |
| AC6 answers cached across runs | I3 | `test_target_lookups_are_cached_across_runs` (second run fetches nothing for targets resolved in the first) | `tests/integration/test_trusted_list_content_publish_integration.py` | integration |
| AC7 unfound target skipped, counted, retried | U7 | `test_unfound_target_is_counted_and_looked_up_next_run` (joins no list; `unresolved_targets` = 1; not cached, so run 2 fetches it and, now present, it joins) | `tests/test_tagging_target.py` | fast |
| AC8 unreachable lookup relays: no content publish, no content retraction, 30392 still published | U8 | `test_unreachable_lookup_relays_skip_content_lists_only` (run result names the reason; publish stub saw 30392s only; retraction stub saw nothing for 30393/30394) | `tests/test_trusted_list_admin.py` | fast |
| AC8 one configured lookup relay down, a hint relay answering: still a failed lookup | U8b | `test_one_configured_lookup_relay_down_is_a_failed_lookup` (two configured relays, one raises; the hint relay answers every target; result as U8, and a previously published content list is **not** republished emptied) | `tests/test_trusted_list_admin.py` | fast |
| AC8 a hint relay down, configured relays answering: view trusted | U8c | `test_hint_relay_down_leaves_the_view_trusted` (content lists publish normally) | `tests/test_trusted_list_admin.py` | fast |
| AC9 dictionary of (Tag, target) pairs, per Observer's web of trust | I4 | `test_two_observers_get_different_content_dictionaries_from_same_taggings` (incl. `trusted_list_min_tag_uses` boundary: N−1 uses absent, N present) | `tests/integration/test_trusted_list_content_integration.py` | integration |
| AC9 taggings without header or Tag element dropped at read, kept at write | I5 | `test_tagging_with_missing_header_or_tag_joins_once_both_arrive` | `tests/integration/test_trusted_list_content_integration.py` | integration |
| AC10 membership and score are story 1's predicate | U9 | `test_content_membership_uses_the_people_predicate[…]` (cutoff boundary; applications == disputes not a member; weighted score < 1 not a member; rigor applied) | `tests/test_trusted_list_content.py` | fast |
| AC10 id and coordinate taggings by different asserters merge into one target | U10 | `test_id_and_coordinate_taggings_count_as_one_target` | `tests/test_trusted_list_content.py` | fast |
| AC10 one stance per asserter per target, newest wins | U10b | `test_one_stance_per_asserter_newest_wins` (apply by id, then dispute by coordinate: counts as one dispute) | `tests/test_trusted_list_content.py` | fast |
| AC10 order: score descending, then key ascending | U10c | `test_content_members_ordered_by_score_then_key` | `tests/test_trusted_list_content.py` | fast |
| AC11 wire shape, both kinds | U11 | `test_content_list_wire_shape[30393, 30394]` (every D5 tag, member tag form `["e"/"a", key, "", "<score>"]`, `content` JSON) | `tests/test_trusted_list_content.py` | fast |
| AC11 no `a`/`p` other than members | U11b | `test_content_list_has_no_back_references` | `tests/test_trusted_list_content.py` | fast |
| AC11 `d` prefix and target suffix | U11c | `test_content_d_tag[notes-k1, items-k30402, items-dl]` (two Tags with the same name and different authors get distinct `d`s) | `tests/test_trusted_list_content.py` | fast |
| AC12 title from DList names, with fallbacks | U12 | `test_content_title[plural, singular-only, d-only]` | `tests/test_trusted_list_content.py` | fast |
| AC12 title from the server's kind table | U12b | `test_content_title_kind_names[1, 20, 30023, 30402, 39998, 39999, 9999]` (asserts the exact table, so a client label like "Photos" or "Open lists" fails it) | `tests/test_trusted_list_content.py` | fast |
| AC12 unnamed kind: no list, reported | U12c | `test_unnamed_kind_gets_no_list_and_is_reported` | `tests/test_trusted_list_admin.py` | fast |
| AC13 signed by the Observer's assistant nsec, on the 30392 relay | I6 | `test_content_lists_signed_by_assistant_on_the_trusted_list_relay` (author-scoped read-back from the isolated TL relay finds them; a wrong signer finds none) | `tests/integration/test_trusted_list_content_publish_integration.py` | integration |
| AC14 one run, three kinds; new result fields | U13 | `test_one_run_builds_three_kinds_and_reports_new_fields` (response carries `kind`, `tagging_target` per list and `unresolved_targets`, `unnamed_kinds` per run) | `tests/test_trusted_list_admin.py` | fast |
| AC14b `/setup` rows straight after `"30392"`, same key and relay | U14 | `test_dictionary_rows_follow_the_trusted_list_row` (edited: Dictionary rows now follow `"30394"`), `test_no_dictionary_rows_without_a_dictionary_relay` (edited: last row is `"30394"`), new `test_content_list_rows_follow_30392_with_its_key_and_relay` | `tests/test_setup_designation.py` | fast |
| AC14b Duty Menu offers the two rows | U14b | `EXPECTED_KEYS` gains `"30393"`, `"30394"` after `"30392"` (every test using it) | `tests/test_assistant_duty_menu.py` | fast |
| AC14b Treasure Map check: two `not-scheduled` duty lines, no self-entries | U14c | `test_setup_s_map_with_scores_live_lists_exactly_the_five_score_rows` (edited: duty keys gain the two; `should_say` unchanged; the Dictionary-relay loop moves from `lines[6:]` to `lines[8:]`; new lines 6 and 7 asserted `not-scheduled`), and the check endpoint's assertion at its `check["duties"]` keys | `tests/test_assistant_treasure_map.py` | fast |
| AC15 content slot retracted when its pair leaves; identity read off the live event | U15 | `test_content_slot_retracted_reading_identity_from_live_event` (slug `a-b` and DList `d` `x-y`, so splitting the `d` would misread it) | `tests/test_trusted_list_retraction.py` | fast |
| AC15 failed publish keeps the content slot; untrustworthy empty view retracts nothing | U15b | `test_failed_content_publish_keeps_its_slot`, `test_untrustworthy_empty_view_never_retracts` (extended to 30393/30394) | `tests/test_trusted_list_retraction.py` | fast |
| AC15 live slot missing or garbling `source-tag`, `tagging-target` or `title`: left in place, logged, run continues | U15c | `test_unreadable_live_slot_is_left_in_place[no source-tag, no tagging-target, no title, source-tag not a coordinate]` (no retraction published for it; another stale, readable slot in the same run is still retracted) | `tests/test_trusted_list_retraction.py` | fast |
| AC15 the live-slot read fails: nothing of any kind retracted, publishing goes ahead | U15d | `test_failed_slot_scan_retracts_nothing_across_kinds` (`_fetch_published_tl_slots` raises; current 30392/30393/30394 lists still publish; zero retractions) | `tests/test_trusted_list_admin.py` | fast |
| AC15 publish → retract → idempotent, all three kinds, on a live relay | I7 | `test_content_publish_retract_idempotence` | `tests/integration/test_trusted_list_content_publish_integration.py` | integration |
| AC16 emptied list republished, no retracted marker | U16 | `test_emptied_content_list_republished_without_marker`; I7 also reads it back off the relay | `tests/test_trusted_list_retraction.py` | fast + integration |
| AC17 people lists unchanged | U17 (sentinel) | `test_people_lists_match_story_1_output` (fixed input → 30392 tags and content equal a golden file captured from the current code before implementation) | `tests/test_trusted_list_content.py`, `tests/fixtures/trusted_list_30392_golden.json` | fast |
| AC17b replay: dry run writes nothing, prints the five counts | U18 | `test_backfill_dry_run_writes_nothing_and_counts` | `tests/test_event_tagging_backfill.py` | fast |
| AC17b replay: unreadable lines skipped | U18b | `test_backfill_skips_unparseable_lines` | `tests/test_event_tagging_backfill.py` | fast |
| AC17b replay: `--apply` writes; a second run changes nothing | I8 | `test_backfill_apply_twice_changes_nothing_the_second_time` | `tests/integration/test_event_tagging_store_integration.py` | integration |
| AC18 live acceptance on the search relay | M1 | the three searches below, recorded in the Gate B review with their output | — | manual |

**Level rationale.** As in story 1: parsing, target resolution over fetched
event dicts, membership, wire shape, titles and the retraction decision are
pure functions and run fast with no mocking. Storage semantics (latest-wins,
dropped-at-read), per-Observer trust (Neo4j `influence_<observer>`), the
lookup cache across runs, and what actually lands on a relay need the real
backends.

**Existing tests edited, not only added.** The AC14b handles change
assertions in three existing files. Each edit fails until the rows exist, so
it counts toward item 4 below. Story 1's tagging tests are **not** edited
(AC5): if one has to change, the change is a finding.

### M1 — live acceptance (after deploy and the replay)

Through the house's perspective, anonymous read (`include:spam`):

```
nak req -k 0 -k 39999 -k 30392 -k 30394 --search "verified human include:spam" wss://search.brainstorm.world
nak req -k 1 -k 30393 --search "stoicism include:spam" wss://search.brainstorm.world
nak req -k 39999 -k 30394 --search "espresso books include:spam" wss://search.brainstorm.world
```

Pass: the first returns profiles and GitHub Accounts items carrying the tag;
the second returns notes tagged stoicism; the third returns a Books item
tagged espresso, once such a tagging exists (it needs Brainstorm-UI#334, the
client tagging addressable events by coordinate, or a tagging made in
Tapestry).

## Edge cases and regression sentinels

Checked items have a handle. Unchecked items are not covered, with a reason.

- [x] **S1 — content lists never enter the people retraction, and the reverse.**
      `test_retraction_scans_are_kind_scoped` (`tests/test_trusted_list_retraction.py`):
      a run whose people dictionary is empty but trustworthy retracts stale
      30392 slots and leaves every 30393/30394 slot alone; a run with a failed
      lookup (AC8) retracts no content slot and still retracts a stale 30392.
      No AC states the isolation in both directions, and a shared scan over
      `kinds=[30392, 30393, 30394]` would break it silently.
- [x] **S2 — one DList with both item kinds gets two lists.** A DList holding a
      39999 item (by `a`) and a 9999 item (by `e`), both tagged with one Tag,
      yields one 30394 and one 30393 with the same title and `tagging-target`.
      `test_dlist_with_both_item_kinds_gets_a_list_of_each`
      (`tests/test_trusted_list_content.py`). From ADR D2's first bullet, not
      from any AC.
- [x] **S3 — `ev_kinds` still only the graph kinds.** Story 1's
      `test_ev_kinds_still_only_graph_kinds` stays as is: the new parsers must
      not tempt anyone into adding 39999 to `ev_kinds`.
- [x] **Tagging before its header, header before its Tag element.** I5.
- [x] **A header republished with a different `a`.**
      `test_repointed_header_moves_its_taggings_next_run`
      (`tests/integration/test_trusted_list_content_integration.py`).
- [x] **An id-tagged addressable event with a newer version.** A U6 case: the
      lookup by id returns the old version, and its coordinate is the member.
- [x] **A deleted target, or one on no lookup relay.** U7.
- [x] **Two Tags with the same name and different authors.** U11c.
- [x] **A tagged Tag.** A U6 case.
- [x] **One asserter, one target, by id and by coordinate.** U10b.
- [x] **More than 100 members.** `test_more_than_100_members_published_whole`
      (`tests/test_trusted_list_content.py`): 150 members, all 150 in the tags
      and `content`.
- [x] **A lookup answer that isn't what was asked for.** A relay returning an
      event whose id, or whose kind, author and `d`, don't match the request is
      ignored and the target stays unresolved:
      `test_lookup_answer_not_matching_the_request_is_ignored`
      (`tests/test_tagging_target.py`).
- [ ] **Not covered — a long DList `d`.** The story asks that the list's `d`
      stay under relay limits (the tag hub rejects filter values over 255
      bytes). ADR 0002 decides no behavior past that limit, so a test could
      only pin today's unbounded concatenation. It belongs with open question
      1 (`d` length, Loom P-141).
- [ ] **Not covered — two pairs producing one `d`.** Hyphens in slugs and in
      DList `d`s make some `d`s ambiguous: slug `foo` with DList `x-k30402`
      and slug `foo-dl<author8>-x` with kind 30402 both give
      `…-foo-dl<author8>-x-k30402`. The same class as the 8-character
      truncation ADR 0001 D5 accepted. Not tested: the fix is a wire change
      (open question 1), and a test would only pin the collision.
- [ ] **Not covered — an item moved to another DList.** A 39999 item
      republished with a different header `z` keeps its cached DList. ADR 0002
      D6 says answers are cached, with no expiry. Flagged for Gate B as a
      design question, not a test gap.
- [ ] **Not covered — concurrent runs for one Observer.** As story 1:
      last-write-wins converges, and no locking is in scope.

## Error paths — one per external dependency

| Dependency | Failure | Handling | Handle |
|---|---|---|---|
| **PostgreSQL** | unreachable during the run | the run fails before publishing any kind | `test_db_failure_aborts_before_publishing` (story 1, extended to assert zero 30393/30394 publishes too) |
| **Neo4j** | unreachable during rank lookup | the same | `test_neo4j_failure_aborts_before_publishing` (story 1, extended likewise) |
| **Lookup relays** (`trusted_list_target_relays`) | all unreachable | content lists skipped and nothing retracted; 30392 still built; reason reported | U8, S1 |
| **Lookup relays** | one configured relay unreachable (the search relay down, a hint relay up) | treated as a failed lookup, as above, so lists aren't emptied by a partial view (ADR 0002 D8) | U8b |
| **Hint relay** (from a tagging) | unreachable | best effort: the view stays trusted | U8c |
| **Trusted List relay (read)** | the live-slot scan fails | nothing of any kind retracted; publishing goes ahead | U15d |
| **Trusted List relay (read)** | a live slot lacks or garbles its identity tags | left in place and logged; the run continues | U15c |
| **Lookup relays** | reachable, target absent | target unresolved, counted, retried | U7 |
| **Lookup relays** | reachable, wrong or forged answer | ignored; target unresolved | `test_lookup_answer_not_matching_the_request_is_ignored` |
| **Search relay as a lookup relay** | an anonymous read without `include:spam` returns nothing | every read to it carries `"search": "include:spam"` | `test_search_relay_lookup_carries_include_spam` (`tests/test_tagging_target.py`) |
| **Trusted List relay (write)** | one list rejected | that list reported failed, the rest continue, its slot stays current | U15b, plus story 1's `test_publish_failure_is_isolated_per_tag` extended to a 30393 |
| **Redis `strfry:events`** | queue empty | unchanged: the consumer blocks as before | not covered: no new code path |
| **Ingest input** | malformed or hostile events | skipped without raising, batch continues | U4, U4b |
| **Replay input** (`strfry scan` on stdin) | blank or unparseable lines, non-39999 kinds | skipped and counted as ignored | U18b |

## Pre-implementation failure spot-check

Per J2 item 4. Today `grep -rn "nostr-event-tag\|tagging-with-specific-tag\|30393\|30394\|event_tagging\|tagging_target" app/ scripts/`
finds only one unrelated index name (`ix_nostr_user_tagging_target`, story 1's
pubkey target column). So:

- **U2** imports the event-tagging parser from `app/services/tagging_parse.py`,
  which has none, and fails at import.
- **U14** fails on current code: `designation_rows` returns no `"30393"` row,
  and the edited `test_dictionary_rows_follow_the_trusted_list_row` expects
  the Dictionary rows after `"30394"`.
- Every other U and I handle imports a module or field that doesn't exist yet
  (`tagging_target_service`, `event_tagging_repo`, `TagResult.kind`, the
  backfill script).

**The deliberate exceptions** pass before implementation by design: U17 (the
30392 golden file is captured from today's code and must still match), S3
(`ev_kinds`), and story 1's unedited tests under AC5. They are sentinels.

## Test infrastructure

- Framework: pytest; the `integration` marker is already registered.
- Fast suite: story 1's `admin_client` fixture and its patched boundaries in
  `tests/test_trusted_list_admin.py` (fake `db_session`, Neo4j session,
  publish and retraction stubs). The lookup is patched at
  `tagging_target_service`'s fetch function, so U6–U8 need no relay.
- Integration suite: `tests/integration/tagging_harness.py`'s per-test engine
  (one `asyncio.run`, own engine, disposed in the same loop), extended to clean
  the three new tables. Real Neo4j as story 1. Two isolated relays: target
  events are published to `bs-test-strfry` and `trusted_list_target_relays`
  points there; lists publish to `bs-test-strfry-tl`, as story 1's I11.
- New fixture: `tests/fixtures/trusted_list_30392_golden.json`, written once
  from the current builder before any implementation commit.
- No new test frameworks or dependencies.

## How to run

The local stack must be up: `bs-test-pg`, `bs-test-neo4j`, `bs-test-redis`,
`bs-test-strfry` and `bs-test-strfry-tl`, with the test database at alembic
head.

```bash
direnv exec . poetry run pytest -m "not integration" -q   # fast suite
direnv exec . poetry run pytest -m integration -q         # integration suite
```

The J3 gate command is the one in story 2's "Gate-command exception
(approved)" section: every `check_all` stage, with exactly the six tests of
NosFabrica/brainstorm_server#129 deselected. Run it in the foreground with the
exit code captured by brace-redirect, never piped through `tail`.
