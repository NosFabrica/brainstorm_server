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
| AC6 by-id answers cached across runs; coordinates fetched fresh | I3 | `test_target_lookups_are_cached_across_runs` (second run fetches no id resolved in the first, and fetches every 39999 coordinate again) | `tests/integration/test_trusted_list_content_publish_integration.py` | integration |
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
| AC11 `d` prefix and target suffix | U11c | `test_content_d_tag[notes-k1, items-k30402, items-dl-coordinate, items-dl-9998-id]` (two Tags with the same name and different authors get distinct `d`s) | `tests/test_trusted_list_content.py` | fast |
| AC11 DList segment, event-taggings rule | U11d | `test_dlist_segment_matches_the_event_taggings_worked_example[…]` (the draft's two full coordinates, `086cb8ff` / `878ce18a`), `test_dlist_segment_keeps_16_characters_of_the_header_d`, `test_dlist_segment_drops_half_a_character_at_the_cut`, `test_two_dlists_of_one_curator_get_two_slots`, `test_slot_length_stays_flat_however_long_the_header_d` | `tests/test_trusted_list_content.py` | fast |
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
      No AC states the isolation in both directions. The one slot scan covers
      all three kinds (ADR 0002 D8); what must stay separate is the decision
      per kind, which a single "trusted / untrusted" flag for the whole run
      would break silently.
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
      (`tests/test_tagging_target.py`). A forged one is dropped:
      `test_a_forged_answer_is_dropped`.
- [x] **A hint pointing inside the cluster.** A tagging's author chooses its
      relay hint; only public `wss://` hosts are dialled:
      `test_only_a_public_wss_hint_is_contacted`,
      `test_an_unusable_hint_is_never_dialled`.
- [x] **A long DList `d`.** The DList segment is bounded by the
      event-taggings rule (ADR 0002 D5), so a header `d` of any length gives
      the same slot length: `test_slot_length_stays_flat_however_long_the_header_d`.
      Only the Tag's slug is still unbounded, as on 30392 (open question 1).
- [x] **Two pairs producing one `d`.** Slug `foo` with DList `x-k30402` and
      slug `foo-dl<author8>-x` with kind 30402 used to give one `d`. A DList
      segment now ends in its hash, so no kind or other DList can share its
      tail short of a hash collision:
      `test_a_hyphenated_slug_no_longer_mimics_a_dlist_slot`.
- [x] **An item moved to another DList.** A 39999 item republished with a
      different header `z` joins the new DList's list on the next run, because
      coordinates are never cached (ADR 0002 D6):
      `test_item_moved_to_another_dlist_follows_it_next_run`
      (`tests/test_tagging_target.py`).
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
| **Lookup relays** | reachable, wrong answer (another id, or another kind, author or `d`) | ignored; target unresolved | `test_lookup_answer_not_matching_the_request_is_ignored` |
| **Lookup relays** | reachable, forged answer (altered content, or a bad signature) | dropped before its facts are read | `test_a_forged_answer_is_dropped` (`tests/test_tagging_target.py`), on the check `_fetch_from_relay` applies to every answer |
| **Hint relay** | malformed, non-`wss://`, IP literal, `localhost`, dotless or cluster-internal | never dialled, logged; configured relays still decide | `test_only_a_public_wss_hint_is_contacted[…]`, `test_an_unusable_hint_is_never_dialled` (`tests/test_tagging_target.py`) |
| **Hint relay** | hangs rather than refusing | not covered: bounded by the connect and fetch timeouts already in `_fetch_from_relay`, and a hint can't decide trust (ADR 0002 D6, D8); a test would only wait out a timer | — |
| **Search relay as a lookup relay** | an anonymous read without `include:spam` returns nothing | every read to it carries `"search": "include:spam"` | `test_search_relay_lookup_carries_include_spam` (`tests/test_tagging_target.py`) |
| **Trusted List relay (write)** | one list rejected | that list reported failed, the rest continue, its slot stays current | U15b, plus story 1's `test_publish_failure_is_isolated_per_tag` extended to a 30393 |
| **Redis `strfry:events`** | queue empty | unchanged: the consumer blocks as before | not covered: no new code path |
| **PostgreSQL** | unreachable while a tagging is ingested | unchanged: the consumer has already popped the message, logs the handler's exception and moves on, as for story 1's pubkey taggings and every other kind; the tagging is recovered by the replay (ADR 0002 D10), which is idempotent | not covered at ingest: no new code path, the consumer's handling is story 1's. The replay's half is covered: `test_backfill_counts_a_failed_event_and_goes_on` (`tests/test_event_tagging_backfill.py`, `--apply` with a failing write: counted, the rest still written) |
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

## Recorded at implementation

Every planned handle exists. Where one landed under another name or file, or
was folded into another, it is listed here; nothing was dropped.

- **I3, I6, I7 are one test,**
  `test_content_lists_publish_cache_retract_and_empty`
  (`tests/integration/test_trusted_list_content_publish_integration.py`), for
  story 1's reason: the service's asyncpg pool and Neo4j driver bind to the
  first event loop that touches them, so one live scenario runs in one loop.
  It asserts the cache (I3), the signer and relay (I6), and publish, retract,
  emptied and idempotent runs (I7, AC16). I7's "all three kinds" is met by a
  person tagged with the same Tag: their 30392 is published beside the two
  content lists and is still live, unretracted, after the content retraction
  and the idempotent fourth run.
- **U12's DList-name cases** are `test_dlist_name[plural, singular-only,
  d-only]` in `tests/test_tagging_target.py`, where `dlist_name` lives, plus
  `test_content_title_from_a_dlist` in `tests/test_trusted_list_content.py`.
- **U12c** is in `tests/test_trusted_list_content.py` (the pure plan), and the
  run result's `unnamed_kinds` is asserted again by U13.
- **U15c and U15** each have a run-level twin in `tests/test_trusted_list_admin.py`
  (`test_unreadable_live_slot_is_left_while_a_readable_one_is_retracted`,
  `test_stale_content_slot_is_retracted_with_its_live_identity`), beside the
  pure ones in `tests/test_trusted_list_retraction.py`.
- **U16 and S1** are in `tests/test_trusted_list_admin.py`, not the
  retraction file: both need a whole run.
- **"Story 1's read-failure tests, extended":** their publish spy already
  asserts that nothing of any kind is published. The content read gets its own
  `test_event_tagging_read_failure_aborts_before_publishing`.
- **"`test_publish_failure_is_isolated_per_tag` extended to a 30393"** is a
  new test, `test_failed_content_publish_is_isolated_and_keeps_its_slot`, so
  story 1's stays as it was.
- **U7** is asserted at both levels: `test_unfound_target_is_counted_and_looked_up_next_run`
  (the lookup) and `test_unresolved_target_is_counted_and_not_cached` (the run).
- **Hint filtering and the forged-answer check** were added after
      implementation (ADR 0002 D6): `test_a_forged_answer_is_dropped`,
      `test_only_a_public_wss_hint_is_contacted`,
      `test_an_unusable_hint_is_never_dialled`; existing tests' hint URL moved
      from `wss://hint` to `wss://hint.example`.
- **Added beyond the plan:** `test_coordinate_lookup_keeps_the_newest_version`,
  `test_nothing_to_look_up_contacts_no_relay`, `test_rigor_is_the_observers`,
  `test_pair_below_min_uses_gets_no_list`, `test_a_neutral_stance_is_no_use`,
  `test_a_9998_header_that_wasnt_found_leaves_its_items_unresolved`,
  `test_foreign_39999_writes_nothing`, the replay script's
  apply-and-failure cases, and `test_the_response_carries_the_new_fields`.
- **The DList slot rule changed after implementation** (Vinney, 2026-11-04: the
      event-taggings rule in ADR 0002 D5). U11c's cases were renamed, U11d is
      new, and the two `d` edge cases above moved from not covered to covered.
- **Story 1's admin tests** gained one autouse fixture holding the event
  taggings at zero, so their runs stay people-only; no story 1 assertion
  changed. `test_untrustworthy_empty_view_never_retracts` gained the event
  tagging counts in both its cases, as planned.
- **Timing in the live test:** a replaceable event is ordered by `created_at`
  in seconds, so a retraction published in the same second as the list it
  replaces can lose to it on the relay ("replaced: have newer event"). The live
  test waits 1.1 s before the run that retracts, as story 1's does. Admin runs
  are never that close; the same property holds for 30392.
