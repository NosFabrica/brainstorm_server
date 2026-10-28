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
| AC10 targets, date, 502 | B18, R14, R15 | `test_a_new_version_is_dated_after_the_newest`, `test_the_copy_goes_to_the_dictionary_relay_and_the_outbox_dated_after_the_newest`, `test_when_no_relay_takes_it_the_answer_is_502` | both |
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
- [ ] **One unknown concept doesn't stop the others** (R19; ADR D6). The request answers 200 with each status.
- [ ] **A concept not sent by the deadline is `failed`, "not sent: out of time", with no event** (R20; ADR D6).
- [ ] **Two presses for one Assistant never send at once** (R21; ADR D7). The service is called concurrently with
  `asyncio.gather`, and the fake counts sends in flight.
- [ ] **Regression sentinel S1:** `tests/test_assistant_duty_menu.py` and `tests/test_assistant_treasure_map.py` stay
  green unchanged. Their relay I/O (`connect_targets`, `send_event`, `relay_report`) is what this story imports,
  untouched.
- [ ] **Regression sentinel S2:** the rest of the fast suite stays green: 1,494 passed on the base (`e2ca872`).
- [ ] **Regression sentinel S3:** the AC11 fixtures are the UI's, verbatim, so the cross-repo constant can't drift
  silently. B1 and B3 fail the moment either side changes alone.

## Error paths, one per external dependency

| Dependency | Failure | Handle | Expected |
| --- | --- | --- | --- |
| Dictionary relay (strict read) | unreachable | W6, R17 | the reader raises; the request answers 502 with nothing sent (both routes) |
| Dictionary relay (strict read) | `CLOSED` | W5 | the reader raises |
| Dictionary relay (strict read) | holds nothing | W4, R10 | `{}`; `not_found` for an add |
| Dictionary relay (strict read) | invalid event | W3 | skipped |
| Publish relays (nostr-sdk `Client`) | every relay refuses | R15 | 502, with a string `detail` |
| Publish relays | one relay refuses | R16 | that line has `ok: false` and the relay's message |
| Publish relays | slow | R20 | the deadline: later concepts `failed`, out of time |
| Request body | malformed | R11 | 422, before any work |
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
- `dictionary_relay_reader`: W1–W6, at collection;
- `assistant_dictionary_service`: R1–R10, R12, R14–R22, at fixture setup, where the fakes are installed at its import
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
- **R20 is timed.** It sets `SEND_DEADLINE_SECONDS = 0.5` and a 1.0 s send delay: the first send starts at about 0
  s, the second at about 1.0 s, so there's 0.5 s of margin on each side.
- **The implementation must expose these seams** (named in the ADR):
  - module-level `Client`, `fetch_newest_headers`, `assistant_relay_list`,
    `get_or_create_brainstorm_observer_nsec_by_pubkey_on_db` and `SEND_DEADLINE_SECONDS` in
    `assistant_dictionary_service`;
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

## Verification

The new tests fail with the current code. Confirmed 2026-10-27 against the tree at `1914e2a`, with only these three
test files added:

```text
tests/test_dictionary_copy_build.py   ERROR at collection: ModuleNotFoundError: No module named 'app.services.dictionary_copy_build'
tests/test_dictionary_relay_reader.py ERROR at collection: ModuleNotFoundError: No module named 'app.services.dictionary_relay_reader'
tests/test_assistant_dictionary.py    16 failed, 22 errors in 0.40s
  FAILED test_a_body_that_isnt_1_to_10_concept_coordinates_is_a_422 (14 cases): assert 405 == 422 / assert 404 == 422
  FAILED test_both_routes_refuse_a_request_without_a_session (2 cases): assert 405 in (401, 403) / assert 404 in (401, 403)
  ERROR  (22 at setup): ModuleNotFoundError: No module named 'app.services.assistant_dictionary_service'
```
