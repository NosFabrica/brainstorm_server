# Story 1: Your Assistant's Dictionary — the Assistant signs concept copies

**Status:** Draft (Gate A approved 2026-10-27)
**Created:** 2026-10-27
**Type:** Feature
**Source spec:** [NosFabrica/brainstorm_server#112](https://github.com/NosFabrica/brainstorm_server/issues/112),
from Brainstorm-UI's `docs/dictionary/ASSISTANT-COPY-ASK.md`
([NosFabrica/Brainstorm-UI#299](https://github.com/NosFabrica/Brainstorm-UI/pull/299))
**Base:** branched from #110 (`feat/settings-updates`) for the Assistant's relay I/O; rebased onto `main` once #110
merges.
**Classification:** ADR (irreversibility triggers: a wire format or event shape, a cross-repo contract, request
routing, an auth/trust default (what the Assistant signs, at whose request), and a value that exists in more than one
repo (the copy rule's tag list)). The story runs the **Standard** phases per
[`workflows/light-profile.md`](../../workflows/light-profile.md) §"Gate A". ADR:
[`decisions/assistant-dictionary/0001-the-assistant-signs-concept-copies.md`](../../decisions/assistant-dictionary/0001-the-assistant-signs-concept-copies.md).

## Background

Every customer has a Brainstorm Assistant: the Observer's dedicated assistant key (`brainstorm_nsec`), the one its
Trusted Lists and Duty Menu are signed with. Since #110:

- `GET /setup/{pubkey}` gives the Assistant the customer's Concepts duty (`39998` and `39999` at
  `settings.dictionary_relay`);
- the Assistant's Duty Menu offers that duty.

But the Assistant publishes no concept header yet, so its Treasure Map lists Concepts as `not-published-here`.

A concept is in someone's **Dictionary** when they hold a **local copy** of it: a kind-39998 header at the community
header's `d`, pointing back with `["b", <community coordinate>, "pointer"]`. Brainstorm-UI already reads Dictionaries
this way: Settings › Dictionary, item pages and search read the customer's own copy first, then their Assistant's.
Its "Add to my Dictionary" (Brainstorm-UI#299) has a line for each Dictionary:

- the customer's own, which they sign in the browser;
- the Assistant's, the preferred one, which only this server can sign.

Until the routes below exist, the Assistant's line says it can't add concepts on this server yet.

The product decision is Brainstorm-UI ADR 0009, accepted 2026-10-27 for Loom's K2.6:

- concepts are added by hand;
- the Assistant is the caller's own;
- the copy is exact, with a `pointer` `b` (Tapestry's header contract, `protocols/drafts/assistant-designation.md` §
  "Per-DList curation entries");
- adding is affiliation, not a vote;
- removing is withdrawing (`["b", "b-tag-deferred"]`), never a NIP-09 deletion.

## User-facing description

As a Brainstorm customer, I want my Brainstorm Assistant to add a community concept to the Dictionary it keeps for
me, and to remove it again, so that the concepts I use are defined by my Assistant's copy without my signing
anything.

## Vocabulary

Per [`CONTEXT.md`](../../../CONTEXT.md), Brainstorm-UI's `CONTEXT.md` ("Concepts and the Dictionary") and Tapestry's
`protocols/drafts/inherit-from.md`:

- **Community concept** — the kind-39998 header other headers point at, named by its coordinate
  `39998:<pubkey>:<d>`. A `d` may contain colons.
- **Local copy** — someone's own kind-39998 header at the community header's `d`, carrying
  `["b", <community coordinate>, "pointer"]`.
- **The Assistant's Dictionary** — the community concepts the Assistant holds a local copy of.
- **Withdrawn copy** — the same coordinate republished with `["b", "b-tag-deferred"]` in place of the pointer:
  "considered, affiliated with none".

## Acceptance criteria

Testable from the outside. Each criterion gets at least one test handle.

- [ ] **AC1 — adding publishes an exact copy.** `POST /user/assistantDictionary` with a community coordinate signs,
      as the caller's Assistant, a kind-39998 event at the community header's `d`. Its tags are, in this order:
      - the community header's `names`, `name`, `description`, `slug`, `required`, `recommended`, `optional`,
        `allowed`, `field-type`, `display`, `image` and `link` tags, verbatim and in the header's order;
      - `["b", <community coordinate>, "pointer"]`;
      - the client tag;
      - nothing else.

      The answer is `published`, with the signed event and a per-relay report.
- [ ] **AC2 — adding is idempotent.** A second add with nothing changed answers `unchanged` and publishes nothing.
- [ ] **AC3 — the copy follows the community when asked.** After the community header changes, an add answers
      `updated` and republishes the same coordinate.
- [ ] **AC4 — never re-points.** When the Assistant's header at that `d` points elsewhere or carries no `b`, nothing
      is published, and the answer is `conflict`, with a sentence. The same holds for a second coordinate with the
      same `d` in one request. *(The same-`d` case was added in design, 2026-10-27, ADR 0001 D4.)*
- [ ] **AC5 — removing withdraws.** `POST /user/assistantDictionary/withdraw`:
      - when the Assistant's copy points at the concept, republishes the coordinate with `["b", "b-tag-deferred"]`
        and the withdrawn `alt`, and answers `withdrawn`;
      - with no copy, or one already withdrawn, answers `unchanged`;
      - when the header there points elsewhere, answers `conflict`.
- [ ] **AC6 — adding after a withdrawal** publishes a fresh copy (`published`).
- [ ] **AC7 — an unknown concept.** A coordinate whose community header can't be found answers `not_found` and
      publishes nothing.
- [ ] **AC8 — input.** A body naming anything other than kind-39998 coordinates answers 422 (the request schema), as
      do an empty list and more than 10 coordinates. A `d` containing colons is kept whole. *(Amended in design, 2026-10-27: 400 became 422, this app's
      request-schema convention, ADR 0001 D1.)*
- [ ] **AC9 — routing.** `GET /user/{pubkey}` answers neither path, and both routes refuse a request without a
      session.
- [ ] **AC10 — where it goes.** The event is published to `settings.dictionary_relay` and the Assistant's outbox
      relays, dated `max(now, newest version + 1)`. The answer is 502 when no relay took it.
- [ ] **AC11 — it matches the UI.** For the fixtures in Brainstorm-UI's `lib/conceptCopy.test.ts`, the server's copy
      has the same tags `exactCopyTemplate`'s test expects, and its withdrawn copy the same tags `withdrawnTemplate`'s
      test expects, apart from the client tag the server adds. *(Extended in design, 2026-10-27, to the withdrawn
      form, also a cross-repo constant.)*

## Approach

Two routes on the authenticated `/user` router, beside the Duty Menu's, built from two modules in the
`duty_menu_build` / `duty_menu_service` split:

- **`dictionary_copy_build`** is pure, with no I/O. It owns the coordinate grammar, the copy's tags and the withdrawn
  tags, and the outcome given what the Assistant already holds.
- **`assistant_dictionary_service`**:
  - reads the community header and the Assistant's newest version from the dictionary relay;
  - signs with the Assistant's key;
  - publishes through the Duty Menu's relay I/O (`connect_targets`, `send_event`, `relay_report`);
  - handles one press at a time per Assistant.

## Gate-command deviation (approved)

On the base (#110's head, `e2ca872`), `poe check_all` is red only where it needs services:

- `black`, `isort`, `flake8` and `mypy` pass (`mypy`: no issues in 192 source files).
- `pytest ./tests`: 1,494 passed, 47 failed and 96 errors.

All 143 failures and errors are under `tests/integration/`, which needs a live Neo4j (`127.0.0.1:7687`), Postgres,
Redis and strfry. None of those runs on the machine building this story: Docker's CLI is installed, but no daemon is
running. `pytest -m "not integration"` deselects exactly those 143 tests.

**Operator decision (Gate A, 2026-10-27):**

- The story's gate is `poe check_all` with the integration tests deselected.
- Parity evidence stands in for the rest: the same 143 integration tests fail before and after this diff.
- This story touches no Neo4j, Postgres or Redis path.

This follows the precedent of the Trusted Lists story (Vinney, 2026-08-26). Bringing up the local stack
(`brainstorm_one_click_deployment`) for a fully green run was the alternative, and it was not chosen.

## Out of scope

- The admin route for the same copies (`POST /admin/dictionary/{observer}`, Brainstorm-UI's
  `docs/dictionary/ADMIN-ASKS.md`).
- Items (curation copies), editing the Assistant's copy, and keeping a copy in step when the community header changes.
  Adding again answers `updated`.
- Treasure Map entries (`["39998:<d>", <Assistant>, <relay>]`) and the self-Map's Concepts line.
- A `GET`: the UI reads the Assistant's copies from relays.

## Open questions (resolved in ADR 0001)

Where each is settled:

1. Strict reads: D3.
2. A per-concept relay report in the Duty Menu's shape: D6.
3. The dictionary relay and the outbox only: D5.
4. Read and sign everything, then send; a deadline on sends: D6.
5. No `alt` on an added copy: D2.

The questions as they were asked:

1. **Strict or lenient reads.** If the dictionary relay doesn't answer, should the route refuse (502) rather than
   publish over a header it couldn't see? The self-Map reads strictly and the Duty Menu leniently. AC4 argues for
   strict.
2. **The answer's `relays`.** The ask lists URLs, while the Duty Menu answers `{url, ok, message}` per relay. Should
   it align on the Duty Menu's shape, with the UI's type in #299 updated to match?
3. **Publish targets.** Beyond the dictionary relay and the outbox, should it also publish to every relay an earlier
   version was found on, as the Duty Menu does?
4. **Several concepts in one call.** One event per concept, with a status per concept. Is the 502 only for when
   nothing was published?
5. **`alt` on an added copy.** The withdrawn copy carries one. Should an added copy describe itself too, or carry
   none, as the UI's own copy does today?

## Implementation scope notes (J3 item 2)

The production diff stays inside ADR 0001's blast radius:

- the three new services;
- two routes in `app/routers/user/router.py`;
- models added to the three schema files;
- rows in `app/services/CLAUDE.md` and `app/routers/CLAUDE.md`;
- the `BIBLE.md` §4 sentence;
- the `self_map_build.py` docstring.

**One test file is outside the blast radius's letter: `tests/test_dictionary_relay_reader.py`.** The ADR declares
"the two test files", `tests/test_dictionary_copy_build.py` and `tests/test_assistant_dictionary.py`. The test plan
added this third one, for the strict reader's W1–W8.

- **Why a separate file:** these tests run the reader over a real local websocket, as `tests/test_designated_pubkeys.py`
  runs `fetch_latest_designation`. The route tests fake the reader at the service's import site, so they can't see a
  socket, a timeout or a close before EOSE.
- **It's test-only.** The test plan declares it: its W handles are in the coverage map and the error-path table, and the
  file itself is in its How to run and Verification sections.
- **It came before the implementation:** it was committed in the test-design phase (`498fc5f`, `1e26204`, `c32c5b6`),
  before the implementation commit.

No test changed during implementation. Three choices go beyond the ADR's letter, inside its intent:

- **A withdraw makes one read, not two.** D3 says "two reads per request". A withdraw needs only the Assistant's own
  headers, since removing a copy doesn't depend on the community header it points at. R22 pins that a copy whose
  community header is gone can still be removed. The one read is still strict, and still on the dictionary relay only.
- **The self-authored rule also covers a withdraw.** D4 states it under "For an add". Withdrawing the Assistant's own
  coordinate would republish a self-declared header as deferred, so it answers `conflict` instead.
- **A schema imports from `app/services/`.** `request_body_schemas.AssistantDictionaryBody` validates with
  `dictionary_copy_build.parse_concept_coordinate`, as the ADR's Implementation notes say. It's the first schema to do
  so. No cycle results: that module is pure, imports only `app/utils/client_tag`, and `app/services/__init__.py` is
  empty.

## Gate results (the approved deviation)

Last run 2026-10-28, after the Gate B re-review's fixes, in the foreground, each stage's exit code captured by
brace-redirect:

| Stage | Result |
| --- | --- |
| `black --check ./app` | 0 |
| `isort --check ./app` | 0 |
| `flake8 ./app --max-line-length 100 --extend-ignore E203` | 0 |
| `mypy ./app` | 0 (no issues in 195 source files) |
| `pytest -m "not integration" ./tests` | 0: 1,615 passed (the base's 1,494 plus this story's 121), 143 deselected |
| `pytest -m integration ./tests` (parity) | 47 failed, 96 errors: the same 143 node ids as the base `e2ca872`, compared line for line |

Earlier runs: 1,591 passed at Gate B (this story's 97), and 1,596 after the review's fixes (102).

## Changes from the Gate B review

The review (`reviews/assistant-dictionary/1-assistant-copies-concepts.md`) asked for one change.

- **Blocking: the coordinate is parsed whole.** `dictionary_copy_build._COORDINATE` used `re.match` with `$`, which
  also took a trailing newline and dropped it from the `d`. The copy's `b` then named a coordinate other than the one
  copied, and the UI couldn't repair it. It now uses `fullmatch` with `DOTALL`. Regression tests are B4's newline
  cases and R26.

Non-blocking fixes made too:

- **The reader verifies a signature before comparing dates,** so an event with a malformed `created_at` is skipped
  (W9). A malformed `tags` or `d` still failed the read until the re-review's fixes, below.
- **R27** pins the UI's full fixture through signing.
- **The withdraw row in `app/routers/CLAUDE.md`** names `failed` and the 502s.
- **ADR D1's claim about how the UI shows a 422 is corrected.**

The review's other non-blocking findings are follow-ups for the operator.

## Changes from the Gate B re-review

The re-review passed, with four non-blocking findings. All four are fixed, each with tests that fail on `43e51cf` for
the defect's own reason. The proof was run on the whole tree at that commit, with these test files copied over. Since
`d_of` is new, B19 ran there against the old reader's `_d_of`.

- **The `_send` warning quotes what it logs.** It logs `{result.local!r}`, `{line.url!r}` and `{line.message!r}`, so a
  newline in a caller's `d` or a relay's message can't start a forged line (R28). Relay text is logged raw elsewhere
  (`duty_menu_service.py`, `self_map_service.py`); that's outside this story.
- **A header's `d` is its first `d` tag's value.** This is how nostr-sdk's `identifier()` and the UI's `tagValue`
  read it. The rule is `dictionary_copy_build.d_of`, which the reader and `copy_tags` both use (B19).
  - A header whose first `d` has no value is at no `d`. It's never read as the header at its next `d` (W10).
  - A `tags` that isn't a list, or a `d` that's a list or an object, is skipped and no longer fails the read (W9's
    new cases).
- **A coordinate must be valid UTF-8.** `parse_concept_coordinate` refuses a lone surrogate (B5; B5b keeps real
  Unicode), so the schema refuses it and no work is done (R29). Its 422 still can't be written:
  - FastAPI's default handler echoes each error's input;
  - Starlette encodes a response without ASCII escapes;
  - so the route answers 500, as every route in the app does for a lone surrogate in a field that fails validation.

  Fixing that means changing the app's `RequestValidationError` handler, which is a follow-up beyond this story.
- **The docs say what the code does.**
  - The note above on W9 is narrowed.
  - ADR D1 now says when the UI can send a bad body, and what a lone surrogate gets.
  - The line counts in `app/services/CLAUDE.md` and the gate table are current.
