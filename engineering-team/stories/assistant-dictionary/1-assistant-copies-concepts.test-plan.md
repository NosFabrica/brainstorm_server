# Test Plan: Story 1 — Your Assistant's Dictionary: the Assistant signs concept copies

**Story:** `engineering-team/stories/assistant-dictionary/1-assistant-copies-concepts.md`
**ADR:** `engineering-team/decisions/assistant-dictionary/0001-the-assistant-signs-concept-copies.md`
**Date:** 2026-10-27

## Coverage map

Every acceptance criterion has at least one handle. The handle prefixes are:

- `B*`: the pure builders (`dictionary_copy_build`);
- `W*`: the strict relay reader, over a real local websocket;
- `R*`: the two routes and the service, with relays faked at the service's import site.

Every handle is in the fast suite, which needs no services.

| Criterion | Handles | Tests | File |
| --- | --- | --- | --- |
| AC1 exact copy, signed by the Assistant, with a per-relay report | B1, B2, B8, R1 | `test_the_copy_is_the_ui_fixture_verbatim_then_the_client_tag`, `test_the_copy_keeps_the_headers_own_order_and_every_repeat`, `test_with_nothing_held_adding_publishes`, `test_adding_publishes_the_exact_copy_signed_by_the_assistant` | `tests/test_dictionary_copy_build.py`, `tests/test_assistant_dictionary.py` |
| AC2 idempotent | B10, B11, R2 | `test_the_same_copy_is_unchanged`, `test_the_client_tag_doesnt_count_as_a_difference`, `test_adding_again_with_nothing_changed_is_unchanged_and_sends_nothing` | both |
| AC3 follows the community when asked | B12, R3 | `test_a_copy_of_an_older_definition_is_updated`, `test_after_the_community_header_changes_adding_again_updates_the_copy` | both |
| AC4 never re-points, including the same `d` twice | B14, R4, R5 | `test_any_other_header_at_that_d_is_a_conflict`, `test_never_overwrites_a_header_that_isnt_its_copy_of_this_concept`, `test_two_concepts_with_the_same_d_in_one_request_copy_only_the_first` | both |
| AC5 removing withdraws | B3, B15, B16, B17, R6, R7, R8 | `test_the_withdrawn_form_is_the_uis_then_the_client_tag`, `test_a_copy_that_points_here_is_withdrawn`, `test_with_nothing_to_withdraw_withdrawing_is_unchanged`, `test_withdrawing_never_touches_a_header_that_isnt_this_copy`, `test_removing_republishes_the_copy_withdrawn`, `test_removing_what_isnt_there_is_unchanged`, `test_removing_never_touches_a_header_that_points_elsewhere` | both |
| AC6 adding after a withdrawal | B9, R9 | `test_a_withdrawn_copy_is_replaced_by_a_fresh_one`, `test_adding_after_removing_publishes_a_fresh_copy` | both |
| AC7 unknown concept | R10 | `test_a_concept_that_cant_be_found_is_not_found_and_nothing_is_published` | `tests/test_assistant_dictionary.py` |
| AC8 input: 422, `d` with colons kept whole | B4, B5, R11, R12 | `test_a_concept_coordinate_splits_at_its_first_two_colons`, `test_anything_else_is_not_a_concept_coordinate`, `test_a_body_that_isnt_1_to_10_concept_coordinates_is_a_422` (7 bodies × 2 routes), `test_a_d_with_colons_is_kept_whole` | both |
| AC9 routing and session | R1, R13 | R1 is answered by the new route (200, not `/{pubkey}`'s 405); `test_both_routes_refuse_a_request_without_a_session` (2 routes) | `tests/test_assistant_dictionary.py` |
| AC10 targets, date, 502 | B18, R14, R15, R24 | `test_a_new_version_is_dated_after_the_newest`, `test_the_copy_goes_to_the_dictionary_relay_and_the_outbox_dated_after_the_newest`, `test_when_no_relay_takes_it_the_answer_is_502`, `test_when_no_publish_relay_can_be_reached_the_answer_is_502` | both |
| AC11 matches the UI (added and withdrawn) | B1, B3 | the UI's `lib/conceptCopy.test.ts` fixtures, verbatim | `tests/test_dictionary_copy_build.py` |

## Edge cases and regression sentinels

These come from the ADR's decisions, not from any acceptance criterion:

- [ ] **A `b` of another type to the same target is `updated` to a pointer** (B13; ADR D4, Tapestry's upgrade path).
- [ ] **A header carrying `b-tag-deferred` and a `b` to elsewhere is a `conflict`, not "withdrawn"** (B14, third case).
  Withdrawn means the deferred `b` and nothing else.
- [ ] **Every `b` type naming the coordinate points at it, including one nobody has defined yet; the deferred `b`
  names nothing** (B6, B7; the UI's `pointsAt`).
- [ ] **A coordinate the Assistant authored itself is a `conflict`** (R18; ADR D4). A copy of itself would be a
  self-declaration.
- [ ] **Withdrawing needs only the Assistant's header** (R22). A copy whose community header has disappeared can
  still be removed.
- [ ] **An event whose signature doesn't verify is skipped, not trusted** (W3; ADR D3).
- [ ] **Reads go to the dictionary relay, never the outbox** (R25; ADR D3). The dictionary relay's state alone
  decides, and the fake records each relay read.
- [ ] **"Newest" means by `created_at`, whichever order the relay sends** (W1, run in both orders).
- [ ] **One unknown concept doesn't stop the others** (R19; ADR D6). The request answers 200 with each status.
- [ ] **A concept not sent by the deadline is `failed`, "not sent: out of time", with no event** (R20; ADR D6).
- [ ] **Two presses for one Assistant never send at once** (R21; ADR D7). The service is called concurrently with
  `asyncio.gather`, and the fake counts sends in flight.
- [ ] **Regression sentinel S1:** `tests/test_assistant_duty_menu.py` and `tests/test_assistant_treasure_map.py` stay
  green unchanged. Their relay I/O (`connect_targets`, `send_event`, `relay_report`) is what this story imports,
  untouched. *(Since the rebase onto `main`, it lives in `assistant_publish`, and both files still pass unchanged.)*
- [ ] **Regression sentinel S2:** the rest of the fast suite stays green: 1,494 passed on the base (`e2ca872`).
- [ ] **Regression sentinel S3:** the AC11 fixtures are the UI's, copied verbatim. B1 and B3 catch the server side
  drifting from them. They can't see the UI: nothing here reads that repo, so a change on the UI side alone leaves them
  green. The UI side's counterparts are its own tests on the same fixtures, `describe("exactCopyTemplate")` and
  `describe("withdrawnTemplate")` in Brainstorm-UI's `client/src/lib/conceptCopy.test.ts`. Both now assert the full
  tags, the withdrawn `alt` sentence included (Brainstorm-UI#299). A change to `COPIED_TAGS`, the fixture or the
  withdrawn `alt` belongs in both repos in the same round, as ADR 0001's Consequences say.

## Error paths, one per external dependency

| Dependency | Failure | Handle | Expected |
| --- | --- | --- | --- |
| Dictionary relay (strict read) | unreachable | W6, R17 | the reader raises `RelayDidNotAnswer`; the request answers 502 with nothing sent (both routes) |
| Dictionary relay (strict read) | the reader's `RelayDidNotAnswer` | R17 | 502 with nothing sent, on both routes. W5–W8 and W13 pin that each way a relay fails to answer raises it |
| Dictionary relay (strict read) | a bug in the reader: any other error | R30 | not a 502: the error reaches the framework as itself, a 500, with nothing sent, on both routes |
| Dictionary relay (strict read) | `CLOSED` | W5 | the reader raises `RelayDidNotAnswer`, with the relay's reason |
| Dictionary relay (strict read) | takes the REQ, then says nothing | W7 | the reader raises `RelayDidNotAnswer` within its bound, not hanging and not returning what it has |
| Dictionary relay (strict read) | closes the socket before EOSE | W8 | the reader raises `RelayDidNotAnswer`, never returns a partial read as if the relay held nothing |
| Dictionary relay (strict read) | sends something that isn't JSON | W13 | the reader raises `RelayDidNotAnswer`, so the request answers 502, not 500 |
| Dictionary relay (strict read) | answers, then never acknowledges the close | W12 | the answer is kept, in about a second, not lost to the read's deadline |
| Dictionary relay (strict read) | holds nothing | W4, R10 | `{}`; `not_found` for an add |
| Dictionary relay (strict read) | invalid event | W3, W11 | skipped |
| Dictionary relay (strict read) | malformed event (`created_at`, `tags`, `d`) | W9 | skipped; the read still answers |
| Dictionary relay (strict read) | a header whose first `d` has no value | W10 | at no `d`; never read as its next `d`'s |
| Publish relays (nostr-sdk `Client`) | one can't be reached (connect fails) | R23 | its line has `ok: false` and the reason; the others still take it |
| Publish relays (nostr-sdk `Client`) | none can be reached | R24 | 502, with a string `detail` |
| Publish relays (nostr-sdk `Client`) | every relay refuses | R15 | 502, with a string `detail` |
| Publish relays | one relay refuses | R16 | that line has `ok: false` and the relay's message |
| Publish relays | slow | R20 | the deadline: later concepts `failed`, out of time |
| Request body | malformed | R11 | 422, before any work |
| Request body | a `d` that isn't UTF-8 (a lone surrogate) | R29, B5 | refused by the schema, before any work; answered 500, because the 422 can't be written (framework-wide) |
| Publish relays | a relay's message, or the `d`, holds a newline | R28 | quoted in the log, so it stays on one line |
| Session | none | R13 | 401 or 403 |
| Postgres (the Assistant key lookup) | the lookup raises | **not covered** | It surfaces as the app's generic 500, the path the Duty Menu's identical lookup already takes. The story adds no handling for it. |
| Neo4j, Redis | — | not touched | No path in this story reads or writes them (ADR blast radius). |

## Pre-implementation failure spot-check

Per J2 item 4, the planned assertions must fail against the code as it is.

**The spot-check is on R11** (`test_a_body_that_isnt_1_to_10_concept_coordinates_is_a_422`). Today
`POST /user/assistantDictionary` answers **405**, because only `public_router`'s `GET /{pubkey}` matches the path.
`/withdraw` answers **404**. So the test fails with `assert 405 == 422` and `assert 404 == 422`: assertion failures,
not import errors. R13 fails the same way (`assert 405 in (401, 403)`).

**Every other handle** fails with a `ModuleNotFoundError` that names exactly one of the three planned modules:

- `dictionary_copy_build`: B1–B18, at collection;
- `dictionary_relay_reader`: W1–W8, at collection (`cannot import name 'dictionary_relay_reader'`);
- `assistant_dictionary_service`: R1–R10, R12, R14–R25, at fixture setup, where the fakes are installed at its import
  site.

None fails for a typo. Each import names a module the ADR plans and the tree doesn't have.

## Test infrastructure

- **Framework:** pytest. The existing `client` fixture (`tests/conftest.py:187-197`) signs a caller in by overriding
  `verify_token`. No new framework or dependency.
- **Fakes, as the Duty Menu's tests do it** (`tests/test_assistant_duty_menu.py`), all at the service's import site
  (`app.services.assistant_dictionary_service`):
  - `Client` and `fetch_newest_headers` are faked by one in-memory `Relays`;
  - `assistant_relay_list` returns two outbox relays;
  - `get_or_create_brainstorm_observer_nsec_by_pubkey_on_db` is an `AsyncMock` returning a generated Assistant key;
  - `get_db` is overridden with a `MagicMock` session.
- **Signing is real:** `nostr_sdk` `EventBuilder` and generated `Keys`, and R1 checks `Event.verify()`.
- **The reader runs over a real socket:** `websockets.serve` on `127.0.0.1:0`, as `tests/test_designated_pubkeys.py`
  runs `fetch_latest_designation`.
  - Each read runs under the test's own **guard**, 3 s for W7 and W8. A read still going then raises `GuardTimeout`,
    never the reader's own error, so W7 and W8 tell a reader that gives up apart from one that hangs.
  - W7 sets the reader's bound, `READ_TIMEOUT_SECONDS`, to 0.3 s. It asserts the reader raised by itself, within
    2 s.
- **R20 is timed.** It sets `SEND_DEADLINE_SECONDS = 0.5` and a 1.0 s send delay: the first send starts at about 0
  s, the second at about 1.0 s, so there's 0.5 s of margin on each side.
- **The implementation must expose these seams** (the ADR names the modules and their roles):
  - module-level `Client`, `fetch_newest_headers`, `assistant_relay_list`,
    `get_or_create_brainstorm_observer_nsec_by_pubkey_on_db` and `SEND_DEADLINE_SECONDS` in
    `assistant_dictionary_service`;
  - a module-level `READ_TIMEOUT_SECONDS` (5.0, D3's bound) in `dictionary_relay_reader`, read when a read starts so a
    test can shrink it;
  - `add_to_assistant_dictionary(db, user_pubkey, concepts)` returning `AssistantDictionaryResult`, whose
    `concepts[i].status` R21 reads.
- **The fixtures are the UI's:** B1 and B3 copy Brainstorm-UI's `client/src/lib/conceptCopy.test.ts` and
  `conceptCopy.ts` `withdrawnTemplate`, verbatim.

## How to run

```bash
# the story's tests
.venv/bin/pytest -q tests/test_dictionary_copy_build.py tests/test_dictionary_relay_reader.py tests/test_assistant_dictionary.py
```

The story's gate is the approved deviation: `poe check_all` with the integration tests deselected. Run each stage in
the foreground, with its exit code captured by brace-redirect, never piped through `tail`:

```bash
{ .venv/bin/black --check ./app; echo "black=$?"; }
{ .venv/bin/isort --check ./app; echo "isort=$?"; }
{ .venv/bin/flake8 ./app --max-line-length 100 --extend-ignore E203; echo "flake8=$?"; }
{ .venv/bin/mypy ./app; echo "mypy=$?"; }
{ .venv/bin/pytest -m "not integration" ./tests -q; echo "pytest=$?"; }
```

Parity evidence for the 143 deselected integration tests is the same set failing before and after, under
`pytest -m integration ./tests`.

## Added at the Gate B review

The review found that the coordinate pattern took a trailing newline and dropped it from the `d`, so the copy's `b`
named another coordinate. These handles pin the fixes. The first three fail against the pre-fix code (`6c0b9f7`'s
`app/`); R27 passes on both, as it guards a later nostr-sdk upgrade, not a current bug.

| Handle | Test | Pins |
| --- | --- | --- |
| B4 (two more cases) | `test_a_concept_coordinate_splits_at_its_first_two_colons` | a coordinate is parsed whole: `…:x\n` is `d` `x\n`, and the coordinate always equals `39998:<pubkey>:<d>` |
| R26 | `test_a_coordinate_with_a_trailing_newline_names_another_d_and_is_never_mispointed` | through the route, such a coordinate names a `d` nobody holds: `not_found`, nothing signed |
| W9 | `test_a_malformed_event_is_skipped_and_the_read_still_answers` | an event with a malformed `created_at` after a valid one is skipped, not a `ValueError` and a 502 (the `tags` and `d` cases came at the re-review) |
| R27 | `test_every_definition_tag_survives_signing_verbatim` | the UI's full fixture (display hints, image, link) survives signing verbatim, and a re-add is `unchanged` |

## Added at the Gate B re-review

The re-review passed, with four non-blocking findings. These handles pin the fixes, and every one fails against
`43e51cf`, the whole tree with these test files copied over. B19 ran there against the old reader's `_d_of`, since
`d_of` is new; B5b passes on both, as it guards real Unicode against the new check.

| Handle | Test | Pins |
| --- | --- | --- |
| R28 | `test_a_newline_in_the_d_or_a_relays_message_never_forges_a_log_line` | the `_send` warning quotes the `d` and the relay's message, so a newline in either stays on one log line |
| B19 | `test_a_headers_d_is_its_first_d_tags_value` | a header's `d` is its first `d` tag's value, as nostr-sdk and the UI read it: a later `d` never stands in, and malformed `tags` or `d` values give none rather than raising |
| W10 | `test_a_header_whose_first_d_has_no_value_isnt_read_as_its_next_ds` | a newer header `[["d"], ["d", "x"]]` is at no `d`, so the reader keeps the real header at `x` |
| W9 (three more cases) | `test_a_malformed_event_is_skipped_and_the_read_still_answers` | a `tags` that isn't a list, and a `d` that's a list or an object, are skipped too, not a `TypeError` and a 502 |
| B5 (one more case) | `test_anything_else_is_not_a_concept_coordinate` | a `d` holding a lone surrogate isn't a coordinate |
| B5b | `test_a_d_beyond_the_basic_plane_is_still_a_concept_coordinate` | an emoji `d` still parses: the check refuses only what isn't UTF-8 |
| R29 | `test_a_coordinate_that_isnt_valid_utf8_is_refused_by_the_schema` | the schema refuses it, so no work is done. Its 422 can't be written (FastAPI echoes the input, and Starlette won't encode it), so the route answers 500, as every route does for such a body |

## Added after the merge with staging

Staging's #135 made `verify_signed_event` the one way the server accepts a signed event from outside, and the reader
now checks through it. W11 fails against `3426eff`, the merge: the reader there doesn't use `verify_signed_event`, so
the test can't patch it (`AttributeError`).

| Handle | Test | Pins |
| --- | --- | --- |
| W11 | `test_each_event_is_checked_through_verify_signed_event` | each event the reader would keep is checked through `verify_signed_event`, at kind 39998 and the author it's filed under, and one it rejects is skipped |

## Added after Enes's review

Enes's review of #122 found that a stalled close lost an answer read in time, and that `_read` answered a bug in the
reader with 502 (the story, § Changes from Enes's review). Against `f0a856f`, with `RelayDidNotAnswer` defined but
never raised:

- W12 fails with `TimeoutError` at its 0.3 s deadline: the close was still inside it.
- W5–W8 and W13 fail on the error the reader raised itself: `RuntimeError`, `ConnectionRefusedError`, `TimeoutError`,
  `ConnectionClosedOK` and `JSONDecodeError`.
- R30 fails with "DID NOT RAISE": `_read` answered the bug with 502.

W5–W8 now expect `RelayDidNotAnswer` rather than any exception. R17 fakes `RelayDidNotAnswer` rather than the four
errors the reader used to raise, so it runs 2 cases rather than 8.

W12 runs on a raw asyncio server, as `test_designated_pubkeys.py` does, since a websockets server acknowledges every
close. It sets `READ_TIMEOUT_SECONDS` to 0.3 s, below the close's own 1 s bound, so a close still inside the deadline
fails it as well as an unbounded one.

| Handle | Test | Pins |
| --- | --- | --- |
| W12 | `test_a_relay_that_stalls_the_close_keeps_its_answer` | a relay that answers, then never acknowledges the close: the answer is kept, in under 3 s (`CLOSE_TIMEOUT_SECONDS` is 1 s), not lost to the read's deadline |
| W13 | `test_a_relay_that_sends_something_that_isnt_json_didnt_answer` | a message that isn't JSON raises `RelayDidNotAnswer`, so the request still answers 502, not 500 |
| R30 | `test_a_bug_in_the_reader_isnt_answered_as_the_relay_failing` | a `TypeError` or `KeyError` from the reader reaches the framework as itself, a 500, not 502 "Couldn't read the Dictionary relay", on both routes, with nothing sent |

## Verification

The new tests fail with the current code. Confirmed 2026-10-27 against the tree at `1914e2a`, with only these three
test files added. Each file was run on its own, since a collection error stops a combined run:

```text
tests/test_dictionary_copy_build.py   exit 2, 1 error: ModuleNotFoundError: No module named 'app.services.dictionary_copy_build'
tests/test_dictionary_relay_reader.py exit 2, 1 error: ImportError: cannot import name 'dictionary_relay_reader' from 'app.services'
tests/test_assistant_dictionary.py    exit 1, 16 failed, 31 errors in 0.58s
  FAILED test_a_body_that_isnt_1_to_10_concept_coordinates_is_a_422 (14 cases): assert 405 == 422 / assert 404 == 422
  FAILED test_both_routes_refuse_a_request_without_a_session (2 cases): assert 405 in (401, 403) / assert 404 in (401, 403)
  ERROR  (31 at setup): ModuleNotFoundError: No module named 'app.services.assistant_dictionary_service'
```
