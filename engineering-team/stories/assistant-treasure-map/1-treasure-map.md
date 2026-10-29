# Story 1: Your Assistant's Treasure Map

**Status:** Built on [NosFabrica/brainstorm_server#110](https://github.com/NosFabrica/brainstorm_server/pull/110), at
`98ec893`, after the audit's fourth round; waiting for Gate B
**Created:** 2026-10-28, after the build
**Type:** Feature
**Source spec:** the UI's ask, Brainstorm-UI `docs/assistant/TREASURE-MAP-ASK.md`, corrected by
NosFabrica/Brainstorm-UI#301. The UI is NosFabrica/Brainstorm-UI#297.
**ADR:** [`assistant-treasure-map/0001`](../../decisions/assistant-treasure-map/0001-what-brainstorms-records-show-is-kept-up-to-date.md)
**Classification:** ADR. The change trips three irreversibility triggers: a wire format (the self-Map event, and the
two routes' bodies), a schema and migration (`brainstorm_nsec.treasure_map_on`), and a cross-repo contract (the UI's
`parseTreasureMapCheck` and `TreasureMapPublishResult` read the answers). Under
[`workflows/light-profile.md`](../../workflows/light-profile.md) § Gate A, that escalates the story to the Standard
phases.

## How this story was run

- **Built first, recorded after.** No Gate A was held, the ADR was written after the build (2026-10-27), and no J1, J2
  or J3 judge ran. This file is the story the ADR's "Owed" list asks for, written from the code and tests at `50443af`
  and brought up to date at `98ec893`. Its test plan is the edge-case list, the error paths and the AC→handle lines
  below, in this one file, as the Light profile's § Artifacts has it.
- **An audit stood where the interior gates would have.** An independent audit of PR 110 (2026-10-28, 27 findings)
  was fixed in `6cc74ca..28175d2` and `50443af`. Its second round (2026-10-29) re-checked `aa4d516` by reverting
  each fix; what it found is in `aa4d516..20b3679`. Its third round re-checked `562110f`; what it found is in
  `562110f..c04f607`. Its fourth round re-checked `1779d70`; what it found is in `1779d70..98ec893`. The PR body's
  "What changed after the audit" lists the fixes, and its Evidence records the mutation checks and a run against six
  throwaway local relays.
- **Tapestry's ids.** The tests ported from Tapestry's two self-map suites carry those suites' ids in their
  docstrings (`[U14]`, `[R5]`, …), for cross-reference. The handles here are test names.
- **Still owed:** David's acceptance of the ADR, and Gate B. Its reviewer ratifies the classification and writes the
  review file, `../../reviews/assistant-treasure-map/1-treasure-map.md`.

## Background

An Assistant's **Treasure Map** is its own kind 10040, naming itself only for the duties it publishes now and keeps up
to date: a *self-Map* (Tapestry's `protocols/drafts/treasure-maps.md` § 9.2; the § numbers below are its). Each
self-entry `[key, A, relay]` says that A publishes at that relay, and keeps up to date, the insights the key wins. A
client that sees a self-entry can switch to that Assistant at once.

The Duty Menu says what the Assistant offers
([`../assistant-duty-menu/1-duty-menu.md`](../assistant-duty-menu/1-duty-menu.md)); the Treasure Map says what is
active. Brainstorm-UI's fifth Account Setup step shows what the caller's Map asks of their Assistant and what of it is
live, what the Assistant's Treasure Map should list, and the version on relays. It has Tapestry's Turn on, Update now
and Turn off. Only the server holds the key and knows what is live, so it decides what to list and signs it.

## User-facing description

As a Brainstorm customer, I want my Assistant to publish its own Treasure Map, naming itself only for the duties it
keeps up to date, so that a client reading my Map can take my Scores from my Assistant at once. And I want to turn
that on, update it, or turn it off from setup.

## Acceptance criteria

Testable from the outside. Each criterion has at least one handle below.

- [x] **AC1 — two authenticated routes.** `GET` and `POST /user/assistantTreasureMap` refuse a caller without a
      token. They are answered by these routes, not by the `/user/{pubkey}` catch-all.
- [x] **AC2 — the GET's answer.** `{data: {assistant, on, duties, shouldSay, relays, writerRelays}}`, camelCase, in
      the shape the UI parses. `on` is the remembered choice. With no Map, or a Map naming only others, there are no
      duties. With no Map, `relays` and `writerRelays` are the Assistant's outbox.
- [x] **AC3 — the offer is read per key, by § 6.2 covering.** A Map of `/setup`'s rows, with Scores live, gets
      exactly the five Score rows, entry for entry. A bare `30382`, `3038x` or `*` narrows to the five metric keys,
      and is never claimed as written. A `30382` metric Brainstorm doesn't publish, and a kind the offer has no row
      for, are `not-offered`. An Alternate has the duty too. A more specific entry for another key is `elsewhere`,
      and is never listed.
- [x] **AC4 — "live" comes from Brainstorm's records.** Scores are `live` only when they have been published,
      something keeps them up to date, and the last publish is no older than two intervals. That something is the
      scheduler (enabled, with a default policy, the caller's policy or the default enabled, and the caller following
      someone) or, for the House Observer, the periodic trigger. Otherwise the Score lines are `offered`, with the
      reason `not-scheduled`, `not-landed`, or `upkeep-stopped` and `lastLandedAt`. The caller's own policy wins over
      the default.
- [x] **AC5 — what is never claimed.** Scores that aren't live are listed nowhere. Trusted Lists (`not-scheduled`)
      and Concepts (`not-published-here`) are never listed. A key shape the engine doesn't reason over is
      `unchecked`: it is never claimed, and it keeps a broader key from claiming it.
- [x] **AC6 — Turn on publishes what the GET says.** `{on: true}`, for Turn on and Update now, signs with the
      Assistant's key a kind 10040 whose tags are the `shouldSay` self-entries and our `client` tag, with empty
      content. Nothing of the version it replaces is kept. With nothing live, it takes the stale entries off.
- [x] **AC7 — Turn off.** `{on: false}` publishes a version that lists none of the Assistant's duties. It needs only
      the Assistant's own versions: it reads neither the caller's Map nor the follow count, so it goes ahead when the
      Map can't be read or names more than 64 keys, or Neo4j is down. The GET fails in those states.
      NosFabrica/Brainstorm-UI#307 (open) offers Turn off beneath that error; until it lands, that Turn off goes
      through the API alone (ADR D4).
- [x] **AC8 — the newest version, and dating after it.** The version compared is the newest by NIP-01 (the latest
      `created_at`, then the lowest id) across every relay read. A version whose signature doesn't verify is never
      the newest: the new one isn't dated after it, and the relays it names aren't followed. A new version is dated
      `max(now, newest + 1)`.
- [x] **AC9 — nothing to change.** When the newest version's self-entries already are the target, in any order, and
      every relay that answered holds it, a press publishes nothing, answers `published: false`, and saves the
      choice. A relay that answered without that version is sent it, unchanged, and the answer is `published: true`
      with it. A relay that didn't answer is left alone.
- [x] **AC10 — where a version goes.** § 9.2's writer relays, each once: the Assistant's outbox, every relay its
      self-entries name, and every relay a self-entry of a version found named, with the TA relay at its internal
      URL. A press never connects to the caller's hints. The GET lists them in `relays`, after the outbox and the
      `shouldSay` relays, for readers. It names in `writerRelays` the relays read by the press that keeps the
      current choice: while on, Update now's (the outbox and the `shouldSay` relays); while off, Turn off's (the
      outbox alone); at public URLs, never a hint.
- [x] **AC11 — the report.** The POST answers `{on, published, event, relays: [{url, ok, message}]}`, one line per
      relay, at public addresses. A relay that couldn't be reached reads `couldn't connect`. When no relay took a new
      version, the answer is a 502 and the choice isn't saved. A repair (AC9) answers `published: true` whether or not
      its relays take it: the version is already on the relay it was read from, and the report shows each failed
      line.
- [x] **AC12 — strict reads.** A caller's Map that can't be read is a 502 for the GET and Turn on, never an empty
      Map. So is a follow count that failed or wasn't read within 5 s of its start, when the caller's Map asks the
      Assistant for Scores and the scheduler would otherwise run the caller. It's read alongside that Map, and
      awaited only then, since otherwise it changes nothing: with Neo4j down, a Map that asks for no Scores still gets
      its GET and its Turn on, and a read that isn't awaited has stopped before the answer. When no relay answers the
      read of the Assistant's versions, a press is a 502, with nothing published or saved, and a relay that sends
      `CLOSED` hasn't answered. One relay answering with none is a finished read.
- [x] **AC13 — the 64-key cap.** A Map naming more than 64 keys is a 422 for the GET and Turn on, never a truncated
      check, and is never checked at all; Turn on publishes nothing. A Map of 64 keys is checked.
- [x] **AC14 — the choice.** `on` is saved to `brainstorm_nsec.treasure_map_on` only at a press's success exits,
      after the send, and committed before the answer. A press that errors saves nothing.
- [x] **AC15 — the body.** It is `{on: <boolean>}` and nothing else: `{}`, `"true"`, `1` and `null` are 422s. The
      request can't choose whose Assistant signs or where the version goes.
- [x] **AC16 — one press at a time.** A second press for the same Assistant while one is under way is refused at
      once, with a 409, which frees nothing: a third while the first runs is refused too. A press that errors lets
      the next one go ahead.
- [x] **AC17 — one migration head.** The migrations, `c5f1d7a9e2b4` among them, have one head.

## Design (ADR 0001, in brief)

The ADR is the design and stands in for the Design note. In brief:

- **Approach:** `app/services/self_map_build.py` (pure) ports Tapestry's self-map engine. The offer is read per key by
  covering (D1), and "live" comes from the publish clock and the scheduler's own inputs (D2). `self_map_service.py`
  runs the two routes. The GET answers what to list and where to look, and the UI reads the published version itself
  (D6). A press reads strictly, signs the self-entries alone, and sends to § 9.2's writer relays (D4), one press at a
  time (D5). The choice is a column (D3).
- **Rejected alternative:** Tapestry's reading of "live", the newest Assistant-signed TA at the relay, taken as a
  heartbeat (D2, E1). Under delta publishing, a quiet, healthy Observer whose scores haven't changed would read
  `upkeep-stopped`, and every check would cost a relay read.
- **Blast radius, as built:**
  - new: `app/services/self_map_build.py`, `self_map_service.py`, and the migration
    `alembic/versions/c5f1d7a9e2b4_add_treasure_map_on.py`;
  - changed: the two routes in `app/routers/user/router.py`; the schemas in `app/schemas/`; the column in
    `app/db_models/__init__.py` and its setter in `app/repos/brainstorm_nsec.py`; `assistant_publish.py`, shared with
    the Duty Menu; `designation_service.fetch_latest`, now strict; and `cronjobs.scheduler.has_grapevine`, the
    scheduler's follows rule, now shared with the check.
  - Consumers: Brainstorm-UI's fifth setup step; Tapestry's § 9.2 readers; GrapeRank's designation pins
    (`fetch_designated_pubkeys`, through `brainstorm_request_service`), which read through the stricter
    `fetch_latest` and still yield `[]` on any failure; and the scheduler.
  - Outside the diff, in Brainstorm-UI (ADR Debt):
    - `describeDesignation` read the self-Map as an activation. Its fix, NosFabrica/Brainstorm-UI#302, had to reach UI
      production before any server published a self-Map, staging's included: an Assistant's outbox names public
      relays, so a staging Assistant's self-Map reaches production readers too. It did, with
      NosFabrica/Brainstorm-UI#306 (merged 2026-10-29).
    - The repair (AC9) and Turn off while the GET fails (AC7) reach the UI with NosFabrica/Brainstorm-UI#307 (open),
      which asks the relays a press reads that it can tell apart in `relays` whether they hold the newest version.
      Until it lands, both go through the API alone. The GET now names the relays read by the press that keeps the
      current choice (`writerRelays`, AC10), and #307's probe is to move to it. Until then, a version missed only by
      an outbox relay other than the TA relay waits for the next press, and if it withdrew a claim, that relay serves
      the withdrawn self-entries meanwhile, with no press on offer (ADR D6).

## Out of scope

As in the ADR:
- keeping the self-Map true on its own;
- counting or describing Maps (§ 9.2), on any side;
- private duties, NIP-85's encrypted `.content`;
- Trusted Lists and Concepts as live duties;
- Dictionary management
  ([NosFabrica/brainstorm_server#112](https://github.com/NosFabrica/brainstorm_server/issues/112)).

## Edge cases and regression sentinels

Checked items have a handle. The rest are not covered, each with a reason.

- [x] **Fullwidth digits.** `\d` and `int()` read `３０３８２` as 30382, so a kind slot has to be ASCII digits:
      `test_a_kind_slot_is_ascii_digits`. No AC names a spelling.
- [x] **Spellings that name nothing.** `39998:`, `39999:`, `39999:dlist-header` and `39999:duty-menu` change nothing:
      `test_the_spellings_that_name_nothing_change_nothing`.
- [x] **The key grammar.** Handles:
      - the most specific key first (an exact kind, then a family, then `*`; a value before an empty segment):
        `test_compare_keys_puts_the_most_specific_first`;
      - the nameless and Concept spellings: `test_parse_key_reads_the_nameless_and_concept_spellings`;
      - self-entries read back normalized, each once, the author in any case, with a missing or empty relay as `""`:
        `test_read_self_entries`.
- [x] **A relay that stalls its close.** The read's 5 s deadline covers the open and the answer, not the close, which
      is bounded at 1 s. A real-socket test:
      `tests/test_designated_pubkeys.py::test_a_relay_that_stalls_the_close_keeps_its_answer_and_costs_a_second`.
- [x] **A press inside the UI's 30 s.** Each step is bounded: the caller's Map and the versions are read in up to
      6 s each, the connect takes up to 4 s and the send 12 s, about 28 s in all; Turn off, which reads no Map, about
      22 s. The follow count, read alongside the caller's Map within 5 s, adds nothing.
      `test_a_press_fits_in_the_uis_30_seconds` sums the bounds, so raising one past the budget fails it, and holds
      the follow count's above 1 s. `test_the_follow_counts_bound_runs_while_the_callers_map_is_read` checks that
      the bound runs from the read's start, not from the await.
- [x] **Open migrations on the same parent.** PR 111 (`d8f2a1c4e6b9`) and PR 113 (`a7c2e9f1d4b3`) add migrations on
      this one's parent, `b4e8d2a6f0c1`. Staging runs PR 113's code, so its database is almost certainly at
      `a7c2e9f1d4b3` (its `alembic_version` would confirm it). Re-pointed onto `c5f1d7a9e2b4`, that revision would
      make `alembic upgrade head` there find itself at head and never add `treasure_map_on`. So whichever PR merges
      second adds an `alembic merge` revision over the two heads, and the third does the same. Tried on staging
      first, this PR runs there through a branch joining it to PR 113's code, with a merge revision of its own. That
      one has to land on `main`, in whichever of PR 110 and PR 113 merges later (only that one holds both parents),
      or staging is stamped to the join's parents before `main`'s next image starts; otherwise `start.sh` stops with
      "Can't locate revision identified by" the join's. Stamped to `main`'s merge revision instead, staging would
      skip PR 111's migration whenever it merged before the later of the two. Until `main` has the join's revision,
      every image staging runs needs it, PR 113's own included, and the way back is two downgrades with the join's
      image. A local alembic run showed each outcome, in all six merge orders.
      `tests/test_alembic_heads.py::test_the_migrations_have_one_head` fails until then: copying PR 113's migration
      in made it fail.
- [x] **The 500 when the signed event fails its own check** (D4).
      `test_a_version_that_fails_its_own_check_is_never_sent` builds a version one self-entry short: the answer is a
      500, with nothing sent or saved. `tests/test_assistant_publish.py` checks each of `sign_checked`'s three checks
      on its own, and an event it can't sign. Before these, removing the checks left the suite green.
- [ ] **Not covered: a commit that fails after the send.** The new version is then on the relays while
      `treasure_map_on` keeps its old value, and the press answers an error. Pressing the same button again finds
      nothing to change and saves the choice. The fakes record the save but never fail the commit.
- [ ] **Not covered: a relay that refused a removal.** It keeps serving the old claim (ADR Debt). That is the relay's
      doing; each later press that reads there sends it the newest version again and reports the refusal, and a
      relay only an earlier version named is sent it once.
- [ ] **Not covered: keeping itself true.** § 9.2 says a key must remove or narrow a self-entry before it stops
      keeping it up to date. Nothing republishes when the answer changes: the UI shows the Map out of date, and the
      customer presses Update now (ADR Debt; Tapestry's story 5).
- [ ] **Not covered: more than one server process.** The set of presses under way is in-process (D5). The server
      runs one replica with one uvicorn worker.
- [ ] **Not covered: a rate limit on the POST.** There is none, as for the Duty Menu.

## Error paths, one per external dependency

| Dependency | Failure | Handling | Handle |
|---|---|---|---|
| PostgreSQL (the Assistant's row, the scheduling policies, the choice) | unreachable | a 500, before any relay is read or sent to | not covered: shared by every `/user` route; a commit failing after the send is above |
| PostgreSQL | no default scheduling policy | the Score lines read `not-scheduled` | `test_scores_are_live_only_while_the_scheduler_keeps_them_up_to_date` (no-default-policy) |
| Neo4j, the follow count (`has_grapevine`) | unreadable | a 502 for the GET and Turn on, when the caller's Map asks the Assistant for Scores and the scheduler would otherwise run the caller; Turn off doesn't read it | `test_a_follow_count_that_cannot_be_read_is_an_error`; `test_the_follow_count_counts_only_when_the_map_asks_for_scores`; `test_the_follow_count_counts_only_when_the_scheduler_would_run_the_caller`; `test_turn_off_reads_neither_the_callers_map_nor_the_follow_count` |
| Neo4j, the follow count | no answer (the driver has no query timeout and waits 30 s to connect) | a 502 within 5 s of the read's start, alongside the caller's Map, with nothing published or saved; a Map that asks for no Scores doesn't wait for it, and the read has stopped before the answer | `test_the_follow_counts_bound_runs_while_the_callers_map_is_read`; `test_a_follow_count_neo4j_never_answers_is_an_error_at_once`; `test_a_follow_count_that_doesnt_count_isnt_waited_for` |
| Our relay, the caller's Map | unreadable | a 502 for the GET and Turn on; Turn off doesn't read it | `test_a_map_that_cannot_be_read_is_an_error_not_an_empty_map`; `test_turn_on_needs_the_callers_map`; `test_turn_off_needs_only_the_assistants_map` |
| Our relay, the caller's Map | more than 64 keys | a 422 for the GET and Turn on, with no check made; Turn off doesn't read it | AC13's handles; `test_turn_off_needs_no_map_even_one_too_big` |
| Writer relays, reading the Assistant's versions | none answers (one that sends `CLOSED` hasn't answered, and is left out) | a 502, with nothing published or saved | `test_when_no_relay_answers_nothing_is_published_or_remembered`; `tests/test_designated_pubkeys.py::test_a_closed_request_is_no_answer` |
| Writer relays, reading | a version whose signature doesn't verify | ignored | `test_a_version_that_does_not_verify_is_never_the_newest` |
| Writer relays, reading | one stalls its close | its answer is kept, and the close costs at most 1 s | `tests/test_designated_pubkeys.py::test_a_relay_that_stalls_the_close_keeps_its_answer_and_costs_a_second` |
| Writer relays, sending | one can't be reached | its line reads `couldn't connect`; the rest are sent to | `test_the_report_names_public_addresses_one_line_per_relay` |
| Writer relays, sending | none takes a new version | a 502; the choice isn't saved | `test_a_version_no_relay_took_is_an_error`; `test_a_press_that_errors_saves_nothing` |
| Writer relays, sending a repair | none takes it | a 200, `published: true`, the failed lines reported and the choice saved: the version is on the relay it was read from | `test_a_repair_no_relay_takes_still_answers_published` |
| The request | a body other than `{on: <boolean>}` | a 422 | `test_the_body_is_one_boolean` |
| nostr-sdk, signing | the event can't be signed, or the signed event isn't what was built, or not the target | a 500, nothing sent or saved | `test_a_version_that_fails_its_own_check_is_never_sent`; `tests/test_assistant_publish.py` (5 tests) |

## AC→handle lines

Handles are test names, all in the fast suite. They are in `tests/test_assistant_treasure_map.py` unless another file
is named.

- AC1 → `test_the_routes_require_authentication` (GET, POST), `test_get_is_the_check_not_a_user_profile`
- AC2 → `test_the_get_is_the_shape_the_ui_parses`, `test_the_get_reads_the_remembered_choice`,
  `test_with_no_map_there_are_no_duties_and_the_relays_are_the_outbox`,
  `test_no_map_or_a_map_naming_only_others_has_no_duties`
- AC3 → `test_setup_s_map_with_scores_live_lists_exactly_the_five_score_rows`,
  `test_a_bare_30382_narrows_to_one_key_per_published_metric`,
  `test_3038x_narrows_to_the_score_rows_and_says_the_rest_is_not_offered`, `test_a_bare_star_is_never_claimed_verbatim`,
  `test_a_30382_metric_brainstorm_does_not_publish_is_not_offered`,
  `test_a_kind_the_offer_has_no_row_for_is_not_offered` (2 cases), `test_an_alternate_has_the_duty_too`,
  `test_a_more_specific_entry_for_another_key_is_elsewhere_and_never_listed`
- AC4 → `test_scores_are_live_only_while_the_scheduler_keeps_them_up_to_date` (7 cases),
  `test_one_missed_run_is_a_brief_gap_and_two_a_stop`, `test_the_callers_own_policy_wins_over_the_default`,
  `test_the_house_observer_is_kept_up_to_date_by_the_periodic_trigger`,
  `tests/test_scheduler.py::test_has_grapevine_is_whether_the_user_follows_someone`,
  `tests/test_scheduler.py::test_a_cycle_admits_only_users_who_follow_someone` (the scheduler's side of the rule)
- AC5 → `test_scores_that_are_not_live_are_listed_nowhere` (3 cases), `test_lists_are_never_claimed`,
  `test_concepts_are_never_claimed` (4 cases), `test_the_concept_reading`, `test_an_unchecked_key_is_never_claimed`,
  `test_an_unchecked_key_keeps_a_broader_one_from_claiming_it`
- AC6 → `test_turn_on_publishes_what_the_get_says_signed_by_the_assistant`,
  `test_a_version_is_the_self_entries_and_the_client_tag`, `test_a_version_s_tags_are_its_self_entries_alone`,
  `test_turn_on_with_nothing_live_takes_the_stale_entries_off`
- AC7 → `test_turn_off_lists_none_of_its_duties`, `test_turn_off_needs_only_the_assistants_map`,
  `test_turn_off_needs_no_map_even_one_too_big`, `test_turn_off_reads_neither_the_callers_map_nor_the_follow_count`
- AC8 → `test_newest_is_nip_01_s`, `test_the_newest_version_across_relays_is_the_one_compared`,
  `test_a_version_that_does_not_verify_is_never_the_newest`, `test_a_new_version_is_dated_after_the_newest_found`
- AC9 → `test_nothing_to_change`, `test_same_self_entries_is_a_set_of_key_and_relay`,
  `test_nothing_to_change_publishes_nothing_and_remembers_the_choice`,
  `test_nothing_to_change_still_sends_the_version_where_it_is_missing`,
  `test_a_relay_holding_an_older_version_is_sent_the_newest`,
  `test_a_relay_that_does_not_answer_is_not_sent_the_version_again`
- AC10 → `test_it_is_sent_to_the_relays_and_every_relay_an_earlier_version_named`,
  `test_a_press_never_connects_where_the_callers_map_points`,
  `test_the_relays_are_the_outbox_then_should_say_then_hints_each_once`,
  `test_while_on_the_get_names_the_relays_update_now_reads`,
  `test_while_on_a_relay_should_say_names_outside_the_outbox_is_named`,
  `test_while_off_the_get_names_the_outbox_alone_as_turn_off_reads`
- AC11 → `test_the_report_names_public_addresses_one_line_per_relay`, `test_a_version_no_relay_took_is_an_error`,
  `test_a_repair_no_relay_takes_still_answers_published`,
  `test_a_press_that_errors_saves_nothing`
- AC12 → `test_a_map_that_cannot_be_read_is_an_error_not_an_empty_map`, `test_turn_on_needs_the_callers_map`,
  `test_a_follow_count_that_cannot_be_read_is_an_error`,
  `test_a_follow_count_neo4j_never_answers_is_an_error_at_once`,
  `test_the_follow_counts_bound_runs_while_the_callers_map_is_read`,
  `test_the_follow_count_is_read_alongside_the_callers_map`,
  `test_the_follow_count_counts_only_when_the_map_asks_for_scores`,
  `test_the_follow_count_counts_only_when_the_scheduler_would_run_the_caller` (4 cases),
  `test_a_follow_count_that_doesnt_count_isnt_waited_for`,
  `test_a_follow_count_given_up_before_it_starts_leaves_nothing_unawaited`,
  `test_the_upkeep_counts_only_when_the_map_asks_for_scores` (7 cases),
  `test_when_no_relay_answers_nothing_is_published_or_remembered`,
  `test_one_relay_answering_with_none_is_a_finished_read`,
  `tests/test_designated_pubkeys.py::test_a_closed_request_is_no_answer`
- AC13 → `test_a_map_of_64_keys_is_checked`, `test_a_map_too_big_to_check_is_a_422_not_a_truncated_check`,
  `test_a_map_too_big_to_check_is_never_checked`, `test_turn_on_over_a_map_too_big_to_check_publishes_nothing`
- AC14 → `test_the_choice_is_saved_after_the_publish_and_committed_before_the_answer` (a publish, nothing to change
  and a repair),
  `test_a_press_that_errors_saves_nothing`, `test_nothing_to_change_publishes_nothing_and_remembers_the_choice`
- AC15 → `test_the_body_is_one_boolean` (4 cases), `test_the_request_cannot_choose_whose_assistant_or_where`
- AC16 → `test_a_second_press_while_one_is_under_way_is_refused`,
  `test_a_refused_press_leaves_the_running_one_its_slot`, `test_a_press_that_errors_lets_the_next_one_go_ahead`
- AC17 → `tests/test_alembic_heads.py::test_the_migrations_have_one_head`

## Checks

Run 2026-10-29 at `98ec893` (this file's last commit changes only docs). Each stage ran in the foreground, with its
exit code captured by brace-redirect, never piped through `tail`.

| Stage | Command | Exit | Result |
|---|---|---|---|
| `check_fmt` | `black --check ./app` (23.12.1) | 0 | 193 files unchanged |
| `check_imports` | `isort --check ./app` (5.13.2) | 0 | clean |
| `check_lint` | `flake8 ./app --max-line-length 100 --extend-ignore E203` (7.3.0) | 0 | clean |
| `check_types` | `mypy ./app` (1.20.1) | 0 | no issues in 193 source files |
| `test` | `pytest ./tests` with CI's `PYTEST_ADDOPTS="-m 'not integration'"` (7.4.4) | 0 | 1558 passed, 143 deselected, none skipped |

`poe check_all` itself then ran too, and exited 0 with the same results. poetry isn't installed here, and poe 0.18
runs each task through it, so a stand-in answered poe's one call (`poetry env info -p`) with a virtualenv whose
black, isort and flake8 are `poetry.lock`'s and whose mypy and pytest are the project's. The project's own virtualenv
has flake8 5.0.4, whose pycodestyle misreads Python 3.12 f-strings: it fails `check_lint` with 121 findings inside
f-strings across 27 files, the same at `562110f` as at `98ec893`. The 143 deselected tests are the integration suite,
which needs the live stack and wasn't run. None of this story's tests is integration-marked.
