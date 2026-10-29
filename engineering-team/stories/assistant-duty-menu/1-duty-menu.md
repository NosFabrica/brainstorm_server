# Story 1: The Assistant's Duty Menu

**Status:** Built on [NosFabrica/brainstorm_server#110](https://github.com/NosFabrica/brainstorm_server/pull/110), at
`50443af`; waiting for Gate B
**Created:** 2026-10-28, after the build
**Type:** Feature
**Source spec:** [NosFabrica/brainstorm_server#108](https://github.com/NosFabrica/brainstorm_server/issues/108). The
UI's ask is Brainstorm-UI `docs/assistant/DUTY-MENU-ASK.md`, for the setup step in NosFabrica/Brainstorm-UI#292.
**ADR:** [`assistant-duty-menu/0001`](../../decisions/assistant-duty-menu/0001-a-per-customer-duty-menu-of-setups-rows.md)
**Classification:** ADR. The change trips three irreversibility triggers: a wire format (the menu event), a
cross-repo contract (the UI compares the published menu with the GET's rows), and `/setup`'s rows, a public answer
any client reads. Under [`workflows/light-profile.md`](../../workflows/light-profile.md) § Gate A, that escalates the
story to the Standard phases.

## How this story was run

- **Built first, recorded after.** No Gate A was held, the ADR was written after the build (2026-10-27), and no J1,
  J2 or J3 judge ran. This file is the story the ADR's "Owed" list asks for, written from the code and tests at
  `50443af`. Its test plan is the edge-case list, the error paths and the AC→handle lines below, in this one file,
  as the Light profile's § Artifacts has it.
- **An audit stood where the interior gates would have.** An independent audit of PR 110 (2026-10-28, 27 findings)
  was fixed in `6cc74ca..28175d2` and `50443af`. The PR body's "What changed after the audit" lists the fixes.
- **Still owed:** David's acceptance of the ADR, and Gate B. Its reviewer ratifies the classification and writes the
  review file, `../../reviews/assistant-duty-menu/1-duty-menu.md`.

## Background

Brainstorm-UI's fourth Account Setup step shows the caller's Brainstorm Assistant's **Duty Menu**: one kind-39999
event, `d` = `duty-menu`, signed by the Assistant's own key. It says which duties the Assistant takes on and at which
relay each is published. The wire format is Tapestry's pre-NIP, `protocols/drafts/duty-menus.md`
(nous-clawds4/tapestry); the § numbers below are its.

Only the server holds the Assistant's key, so it builds and signs the menu. The UI reads the published menu from
relays and compares it with what the server offers.

## User-facing description

As a Brainstorm customer, I want my Assistant to publish a Duty Menu saying what it takes on and where, so that any
Nostr client can find where my Assistant publishes, and the setup step can show whether the published menu is
current.

## Acceptance criteria

Testable from the outside. Each criterion has at least one handle below.

- [x] **AC1 — two authenticated routes.** `GET` and `POST /user/assistantDutyMenu` refuse a caller without a token.
      They are answered by the menu routes, not by the `/user/{pubkey}` catch-all.
- [x] **AC2 — the offer is `/setup`'s rows.** The GET answers `{assistant, rows}`: the caller's Assistant, and the
      rows `GET /setup/{pubkey}` serves as `[key, relay]`, in the same order, at public relay URLs. The Score keys
      are one `30382:<metric>` per metric a TA carries. A row with no relay is offered by neither.
- [x] **AC3 — `/setup` gains the Dictionary kinds.** `/setup` lists `39998` and `39999` right after `30392`, as bare
      kinds for the same Assistant, at `dictionary_relay`. It leaves them out when that setting is empty.
- [x] **AC4 — the menu's shape.** The POST signs, with the Assistant's key, one kind-39999 event with `d` =
      `duty-menu`, Nous' header in a `z` tag, one `p` equal to its author, one `duty` row per offered row, and our
      `client` tag. It carries nothing else a menu can: no storefront, exceptions, `name` or details. Its `duty`
      rows, as a set of `(key, relay)`, equal the GET's `rows`.
- [x] **AC5 — § 6.1's test.** `is_duty_menu` accepts such a menu. It rejects one with another kind, author or `d`,
      no `z`, no `p`, a `p` that isn't the author, two `p`s, `["z", "list"]`, or a `z` naming a concept header.
- [x] **AC6 — dated to replace.** `created_at` is now when no version is dated later, and one second after the
      newest version found otherwise. So a second publish replaces the first.
- [x] **AC7 — where a version goes.** Each relay once: each row's relay, the Assistant's outbox, every relay a
      `duty` row of an earlier version named, and Nous' relay. The TA relay's public URL is written at its internal
      URL.
- [x] **AC8 — earlier versions are read strictly.** They are looked for at each row's relay, the Assistant's outbox
      and Nous' relay. When no relay answers, nothing is published and the answer is a 502. One relay answering is
      enough. An earlier version whose signature doesn't verify is ignored: the new one isn't dated after it, and the
      relays it names aren't added.
- [x] **AC9 — the report.** The POST answers `{event, relays: [{url, ok, message}]}`, one line per relay, at public
      addresses. A relay that couldn't be reached reads `couldn't connect`. When no relay took the event, the answer
      is a 502.

## Design (ADR 0001, in brief)

The ADR is the design and stands in for the Design note. In brief:

- **Approach:** the rows are one list, `duty_menu_build.designation_rows`. `/setup` serves it, and `offer_rows`
  turns it into the offer (D3). The server builds and signs the menu from that list and its settings; the request
  carries nothing, and the UI only compares (D2). A publish reads the earlier versions strictly, dates after the
  newest, checks the signed event, and sends it where § 8.1 says (D5, D6).
- **Rejected alternative:** the UI builds the template and the server only signs (D2, option B). The rows would then
  come from the request, and the server would have to re-validate every tag to sign nothing it doesn't offer.
- **Blast radius, as built:**
  - new: `app/services/duty_menu_build.py`, `duty_menu_service.py`, and `assistant_publish.py` (shared with the
    Treasure Map);
  - changed: the two routes in `app/routers/user/router.py`, `/setup` in `app/routers/setup/router.py`, the schemas
    in `app/schemas/`, `dictionary_relay` in `app/core/config.py` and `env.example`, and `ta_signing.TA_METRIC_TAGS`.
  - Consumers: `/setup`'s public answer, which changes (D4); Brainstorm-UI's setup step; the Treasure Map, which
    reads `offer_rows`; and Tapestry's menu readers.

## Out of scope

As in the ADR:
- storefronts, `not-offered` rows, and a menu's `name`/`json` details;
- republishing when the offer changes;
- publishing Concepts ([NosFabrica/brainstorm_server#112](https://github.com/NosFabrica/brainstorm_server/issues/112));
- what the Assistant has live, which is the Treasure Map's story
  ([`../assistant-treasure-map/1-treasure-map.md`](../assistant-treasure-map/1-treasure-map.md)).

## Edge cases and regression sentinels

Checked items have a handle. The rest are not covered, each with a reason.

- [x] **The `p` survives signing.** nostr-sdk drops a `p` naming the signer unless `allow_self_tagging()` is set.
      `test_the_menu_is_signed_by_the_assistant_and_shaped_exactly` asserts the one `p`. Without `self_tagging=True`
      the check refuses the event and the test fails. No AC says how the `p` survives signing.
- [x] **A relay spelled with its default port.** nostr-sdk reports `wss://Relay.Example:443` as
      `wss://relay.example`, and a publish has to match that spelling to know which targets connected. Handles:
      - `test_a_relay_spelled_with_its_default_port_is_sent_to`;
      - `test_relay_key_matches_nostr_sdk_s_spelling` (7 cases, checked against nostr-sdk itself);
      - `test_relay_key_keeps_what_tells_relays_apart`;
      - `test_the_public_ta_relay_with_its_default_port_is_written_internally`.

      No AC names a spelling.
- [x] **The route order.** `/user/{pubkey}` is a single-segment catch-all under the same prefix, and the UI reads any
      200 that isn't `{assistant, rows}` as "not on this server yet". `test_get_is_the_offer_not_a_user_profile`
      fails if the menu routes are mounted after it.
- [ ] **Not covered: the 500 when the signed event fails its own check** (D6). No test makes nostr-sdk sign something
      other than what was built. If it did, the shape tests would fail, but nothing asserts that the answer is a 500
      with nothing sent. A test that patches the signer would close this.
- [ ] **Not covered: an earlier version that is validly signed but fails § 6.1.** `_is_own_menu` reads it anyway, so
      a broken menu is still replaced, but no test publishes over one.
- [ ] **Not covered: two publishes at once.** No lock is needed. Both build the same rows from the same settings, so
      they sign the same menu: the same event if they share a second, and otherwise the later replaces the earlier.
- [ ] **Not covered: a rate limit on the POST.** There is none, as for `POST /user/assistantProfile` (ADR Debt).

## Error paths, one per external dependency

| Dependency | Failure | Handling | Handle |
|---|---|---|---|
| PostgreSQL (the Assistant's row, made on first use) | unreachable | a 500, before any relay is read or sent to | not covered: shared by every `/user` route, and nothing is sent before the row is read |
| Relays, reading earlier versions | none answers, or one sends `CLOSED` | a 502, nothing published | `test_when_no_relay_answers_the_read_nothing_is_published`; `tests/test_designated_pubkeys.py::test_a_closed_request_is_no_answer` (the reader the menu shares) |
| Relays, reading earlier versions | a version whose signature doesn't verify | ignored | `test_an_earlier_menu_that_does_not_verify_is_ignored` |
| Relays, sending | one can't be reached | its line reads `couldn't connect`; the rest are sent to | `test_the_report_names_public_addresses_one_line_per_relay` |
| Relays, sending | none takes the event | a 502 | `test_a_menu_no_relay_took_is_an_error` |
| Settings | `dictionary_relay` empty, or a row with no relay | the row isn't served or offered | `tests/test_setup_designation.py::test_no_dictionary_rows_without_a_dictionary_relay`; `test_a_row_with_no_relay_is_not_offered` |
| nostr-sdk, signing | the signed event isn't what was built | a 500, nothing sent | not covered (above) |

## AC→handle lines

Handles are test names, all in the fast suite. They are in `tests/test_assistant_duty_menu.py` unless another file is
named.

- AC1 → `test_the_menu_routes_require_authentication` (get, post), `test_get_is_the_offer_not_a_user_profile`
- AC2 → `test_the_offer_is_the_setup_rows_in_order`, `test_the_offer_names_public_relays`,
  `test_a_row_with_no_relay_is_not_offered`,
  `tests/test_setup_designation.py::test_trusted_assertion_rows_are_still_metric_parameterised`,
  `tests/test_ta_signing.py::test_ta_tags_writes_the_metric_tags_in_ta_metric_tags`
- AC3 → `tests/test_setup_designation.py`: `test_dictionary_rows_follow_the_trusted_list_row`,
  `test_dictionary_rows_name_the_dictionary_relay`, `test_no_dictionary_rows_without_a_dictionary_relay`
- AC4 → `test_the_menu_is_signed_by_the_assistant_and_shaped_exactly`, `test_the_published_rows_are_the_offer`,
  `test_no_menu_semantics_beyond_rows`
- AC5 → `test_6_1_accepts_a_menu`, `test_6_1_rejects` (9 cases)
- AC6 → `test_created_at_is_now_when_nothing_newer_is_out`, `test_created_at_is_bumped_past_the_newest_menu_found`,
  `test_a_second_publish_replaces_the_first`, `test_created_at_rule`
- AC7 → `test_it_is_sent_to_every_place_8_1_names`, `test_publish_targets_are_each_relay_once`,
  `test_the_public_ta_relay_with_its_default_port_is_written_internally`
- AC8 → `test_earlier_versions_are_looked_for_before_dating_the_new_one`,
  `test_when_no_relay_answers_the_read_nothing_is_published`, `test_one_relay_answering_the_read_is_enough`,
  `test_an_earlier_menu_that_does_not_verify_is_ignored`
- AC9 → `test_the_report_names_public_addresses_one_line_per_relay`, `test_a_menu_no_relay_took_is_an_error`

## Checks

Run 2026-10-28 at `50443af` (this file's commit changes only docs). Each stage ran in the foreground, with its exit
code captured by brace-redirect, never piped through `tail`.

| Stage | Command | Exit | Result |
|---|---|---|---|
| `check_fmt` | `black --check ./app` (23.12.1) | 0 | 193 files unchanged |
| `check_imports` | `isort --check ./app` (5.13.2) | 0 | clean |
| `check_lint` | `flake8 ./app --max-line-length 100 --extend-ignore E203` (7.3.0) | 0 | clean |
| `check_types` | `mypy ./app` (1.20.1) | 0 | no issues in 193 source files |
| `test` | `pytest ./tests` with CI's `PYTEST_ADDOPTS="-m 'not integration'"` (7.4.4) | 0 | 1526 passed, 143 deselected, none skipped |

**Deviation:** poe itself didn't run, because poetry isn't installed here. The five stages of `poe check_all` ran
directly with its arguments, as CI's `build.yml` runs them. The 143 deselected tests are the integration suite, which
needs the live stack and wasn't run. None of this story's tests is integration-marked.
