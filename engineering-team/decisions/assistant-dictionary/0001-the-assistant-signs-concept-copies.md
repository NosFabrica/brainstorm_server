# ADR 0001: The Assistant signs concept copies on request

**Status:** Accepted (2026-10-27)
**Date:** 2026-10-27
**Story:** `engineering-team/stories/assistant-dictionary/1-assistant-copies-concepts.md`

## Context

Brainstorm-UI's Dictionary lets a customer add a community concept to the Dictionary their Brainstorm Assistant keeps
(Brainstorm-UI#299; ask: `docs/dictionary/ASSISTANT-COPY-ASK.md`; issue #112). A concept is in a Dictionary when its
keeper holds a **local copy**: a kind-39998 header at the community header's `d`, carrying
`["b", <community coordinate>, "pointer"]`. The customer signs their own copy in the browser. Only this server holds
the Assistant's key (`brainstorm_nsec`, read through `get_or_create_brainstorm_observer_nsec_by_pubkey_on_db`).

Brainstorm-UI ADR 0009 (accepted 2026-10-27, Loom K2.6) sets the product rules:

- concepts are added by hand;
- the Assistant is the caller's own;
- the copy is exact, with a `pointer` `b`, as Tapestry's header contract has it (`protocols/drafts/assistant-designation.md`
  § "Per-DList curation entries": the header "carries `["b", <community header a-tag>, "pointer"]`", nothing is
  inherited live, and it "SHOULD copy the community header's names, description, and schema at creation");
- adding is affiliation, not a vote;
- removing is withdrawing (`["b", "b-tag-deferred"]`), never NIP-09.

Repo facts this builds on (on #110's branch):

- **Relay I/O for Assistant-signed events.** `duty_menu_service.connect_targets`, `send_event` and `relay_report`,
  with `duty_menu_build.publish_targets` / `relay_key` / `RelayTarget`, are shared by the Duty Menu and the Assistant's
  Treasure Map. `relay_report` answers `AssistantRelayResult(url, ok, message)` per relay.
- **Strict reads.** `designation_service.fetch_latest_designation` is a plain websocket REQ-until-EOSE that raises on
  a transport error, a timeout or a `CLOSED`, so a relay that didn't answer never reads as one holding nothing. Its
  docstring records why not nostr-sdk: the `Client` returns before its socket is open, so an immediate
  `fetch_events` silently comes back empty. It reads kind 10040 only.
- **One press at a time.** `self_map_service._one_press_at_a_time` is an in-process `asyncio.Lock` per Assistant, so
  two presses can't sign versions with the same `created_at`.
- **The client tag.** `utils/client_tag.client_tag()` is stamped on every event this server signs.
- **The dictionary relay.** `settings.dictionary_relay` (`wss://dcosl.brainstorm.world`) is where `/setup` points the
  Concepts duty, and where Brainstorm-UI reads copies.
- **The outbox.** `assistant_profile_service.assistant_relay_list()` lists the Assistant's outbox relays.
- **Routing.** `routers/router.py` mounts the authenticated `/user` router before `public_router`, whose `/{pubkey}`
  is GET-only. A POST to an unregistered `/user/<name>` therefore answers 405, which Brainstorm-UI reads as "not on
  this server yet".

Invariants:

- **The copy rule is a cross-repo constant.** The UI's `exactCopyTemplate` and `COPIED_TAGS` (`client/src/lib/conceptCopy.ts`)
  and this server must produce the same tags, or the same customer sees two different "exact" copies. Three more
  values live in both repos:
  - the withdrawn form, `b-tag-deferred` and its `alt` sentence (`withdrawnTemplate`);
  - the status words, which the UI's `AssistantConceptResult.status` lists (`client/src/services/api/users.ts`);
  - this ADR's error statuses, 422 and 502.
- **The Assistant never overwrites a header it can't see, or one that isn't its copy of this concept** (the story's
  AC4; Tapestry: "A writer MUST NOT silently re-point an existing header's `b`").

## Options considered

### Option A — The server composes the copy from the community header

The client names community coordinates. The server reads each header, composes the copy by the shared rule, compares it
with what the Assistant already holds, and signs.

- **Pros:** the Assistant only ever says what this server decided it says. Nothing a client sends becomes signed tags.
  Any client (Brainstorm-UI, a script, a future admin route) gets the same copy.
- **Cons:** the copy rule lives in two repos, so a parity test has to guard it (AC11).

### Option B — The client composes the tags, and the server validates and signs them

The UI already runs `exactCopyTemplate`, so it could send the tags, and the server would check their shape and sign.

- **Pros:** one composer.
- **Cons:**
  - The server becomes a signing oracle for kind-39998 headers under the Assistant's name, bounded only by its
    validator.
  - Proving the tags are the community's means reading the community header anyway, so Option A's read happens
    regardless.
  - Every other client would need its own composer.

### Option C — A general "sign as my Assistant" endpoint

A NIP-46-style signer for the caller's Assistant, with a policy on kinds and shapes.

- **Pros:** later duties could reuse it.
- **Cons:**
  - It changes an auth/trust default: what the Assistant may say becomes a policy question for every kind.
  - Its blast radius is the whole Assistant identity.
  - It's far beyond this story.

## Decision

We chose **Option A**: the Assistant should say only what the server composed. The read it costs is one the safe
outcome rules (D4) need anyway.

### D1 — Two POST routes on the authenticated `/user` router

- **The routes:** `POST /user/assistantDictionary` (add) and `POST /user/assistantDictionary/withdraw` (remove), beside
  the Duty Menu's on `router`, never `public_router`.
- **The body:** `{ "concepts": [<coordinate>, …] }`, 1 to 10 coordinates, validated by the request schema
  `AssistantDictionaryBody`.
  - A coordinate is `39998:<64 lowercase hex>:<d>`, split at the first two colons only, so a `d` may contain colons
    and must be non-empty (`parse_concept_coordinate`). *(Since the Gate B re-review it must also be valid UTF-8: a
    lone surrogate, which only a JSON `\ud800` escape can bring, is no coordinate.)*
  - A body that fails the schema answers **422**, before any work. That covers a missing or non-list `concepts`, an
    empty list, more than 10 coordinates, and any coordinate that doesn't parse. 422 is this app's convention for
    request-schema errors (`app/routers/shorturl/CLAUDE.md`; `tests/test_assistant_treasure_map.py` asserts it for a
    `/user` sibling). *(Found at the Gate B re-review: for a lone surrogate the 422 can't be written, since FastAPI's
    default handler echoes the input and Starlette won't encode it, so that body answers 500, still before any work.
    The same holds for every route in the app; the fix is the app's `RequestValidationError` handler, a follow-up.)*
  - The ask and AC8 said 400, and AC8 is amended to 422. *(Corrected at Gate B review: Brainstorm-UI's
    `extractApiError` passes a 422's `detail` list through as the message, so it would show "[object Object]"
    where a 400's string would read. The UI sends a bad body only for a community header without a `d` value:
    `definitionOf` sends `coordinateOf(header) ?? header.id`, which is `39998:<pk>:` or an event id. That's rare,
    and a UI-side fix to `extractApiError` is a follow-up.)*
  - Duplicates are folded after validation, keeping the first.
  - The cap of 10 bounds how many concepts one request can leave `failed`. The send deadline is what bounds the
    time (D6).
- **The response:** both answer `{ "data": { "assistant", "concepts": [ … ] } }`, wrapped like the Duty Menu's.

### D2 — The copy's tags: the shared rule, verbatim

`dictionary_copy_build.copy_tags(community_event, coordinate)` returns, in this order:

1. `["d", <community d>]`;
2. every tag of the community header whose name is in `COPIED_TAGS`, verbatim and in the header's order;
3. `["b", <coordinate>, "pointer"]`;
4. the client tag.

`COPIED_TAGS` is `names`, `name`, `description`, `slug`, `required`, `recommended`, `optional`, `allowed`,
`field-type`, `display`, `image`, `link`. It is copied from Brainstorm-UI's list, with a comment naming that file, the
way Trusted Lists' ADR 0001 D8 copies its concept `z` addresses: a cross-repo constant, copied verbatim and named at
its source.

- **The event's `content` is `""`,** as the UI's copy's is.
- **No `alt`** on an added copy. The UI's own copy carries none, and its names and description say what it is.
- **The withdrawn tags** (`withdrawn_tags(d)`) are `["d", <d>]`, `["b", "b-tag-deferred"]`, the UI's `alt` sentence
  ("A withdrawn concept copy: considered, and affiliated with no community concept.") and the client tag.

### D3 — Strict reads, from the dictionary relay, with signatures checked

A new strict reader, `fetch_newest_headers(relay, authors, ds)`, follows `fetch_latest_designation`'s pattern: one
websocket, REQ until EOSE, and a raise on a transport error, a timeout or a `CLOSED`, with the same 5-second bound. It
returns the newest kind-39998 event for each (author, `d`) pair found, among events whose signature verifies
(`nostr_sdk.Event.from_json(...).verify()`). Invalid events are skipped.

- **Two reads per request, on `settings.dictionary_relay`,** whatever the number of concepts:
  - one for the community headers: `authors` is the coordinates' pubkeys and `#d` their `d`s, matched back to each
    coordinate by its exact (author, `d`) pair;
  - one for the Assistant's headers at those `d`s.
- **A read that raises fails the whole request with 502:** "Couldn't read the Dictionary relay, so nothing was
  published." Reads come before anything is signed or sent (D6), so that sentence is true. The lenient alternative,
  the Duty Menu's, would date correctly but could publish over a header it never saw (the AC4 invariant).
- **Outbox relays are publish targets only,** never read. The dictionary relay is where copies are read, so it's the
  one whose state decides.

### D4 — The outcome rule

`points_at(event, coordinate)` is true when any `b` tag on the event names the coordinate, whatever its type. That's
Brainstorm-UI's `pointsAt`, and `b-tag-deferred` names nothing.

For an add, with the community header found:

| The Assistant's newest header at `d` | Status | What happens |
| --- | --- | --- |
| none, or withdrawn (its `b` is `b-tag-deferred` and nothing else) | `published` | publish |
| points here, and its tags other than `client` equal the composed tags other than `client` | `unchanged` | publish nothing |
| points here, with any other difference, including a `b` of another type to the same target | `updated` | republish (Tapestry's upgrade path) |
| anything else: a `b` to something else, or no `b` | `conflict` | publish nothing, with a sentence |

Four more cases come before the table:

- **A `d` already named earlier in the same request:** `conflict` ("This request already names a concept at that
  `d`."), neither signed nor sent. The Assistant holds one header per `d`, so `39998:A:x` and `39998:B:x` can't both
  be its copy. Deciding both against the one header read in step 2 would sign two events at `39998:<Assistant>:x`,
  and one would silently replace the other. The same rule applies to a withdraw.
- **Community header not found:** `not_found`, and nothing is published.
- **A coordinate authored by the Assistant itself:** `conflict`. A copy of itself would point its own `b` at its own
  coordinate, a self-declaration.
- **A header found under another author's coordinate** is copied as named. The server doesn't judge which concepts
  are "community" ones; the customer asked.

For a withdraw:

| The Assistant's newest header at `d` | Status | What happens |
| --- | --- | --- |
| points at the coordinate | `withdrawn` | republish with the withdrawn tags |
| none, or already withdrawn | `unchanged` | publish nothing |
| anything else | `conflict` | publish nothing |

### D5 — Where the event goes, and when it's dated

- **Targets:** `publish_targets([settings.dictionary_relay], assistant_relay_list())`, through `connect_targets` and
  `send_event`.
- **Date:** `created_at` is `max(now, newest.created_at + 1)` over the Assistant's newest header read in D3, so a new
  version always replaces it.
- **The self-check:** every signed event is checked against what was composed (kind 39998, its `d`, its `b`, and
  `event.verify()`). All checks finish before the first event is sent (D6), so a mismatch answers 500 with nothing
  sent.

### D6 — Several concepts: read and sign everything, then send

A request runs in four steps, all under one press lock (D7):

1. **Validate** the body (D1). A failure answers 422.
2. **Read** the community headers and the Assistant's headers (D3). A failure answers 502, and nothing has been
   signed.
3. **Decide and sign** each concept, in order:
   - compose the copy (D2);
   - take its outcome (D4);
   - for each that publishes, sign and self-check it (D5). A failure answers 500, and nothing has been sent.
4. **Send** the signed events in order, over one connected client.
   - A send starts only while fewer than 40 seconds have passed since the press lock was taken. A concept not sent by
     then answers `failed` ("not sent: out of time").
   - **The budget:** connecting (4 s, `CONNECT_TIMEOUT`), plus two reads (5 s each), plus sends that start before
     40 s, each bounded at 12 s (`SEND_TIMEOUT_SECONDS`). That's at most about 52 s, inside the UI's 60-second wait.
     The Duty Menu's sends are sequential too, and nothing in this codebase sends nostr-sdk events concurrently.
   - **A press that first waits for another press** can take longer than the budget. Brainstorm-UI then shows a
     timeout, and a reload shows what was published. Two presses for one Assistant at once need two tabs, since the UI
     disables its buttons while one is out.

The answer:

- **Each concept** answers `{ community, local, event, status, error, relays }`:
  - `local` is `39998:<assistant>:<d>`, or null for `not_found`;
  - `event` is the signed event, or null when nothing was published;
  - `relays` is the `relay_report` lines for that event, or `[]`.
- **Failure:** a concept whose event no relay took answers `failed`, with a sentence. When the request needed at
  least one publish and none reached any relay, the request answers **502**, as the Duty Menu does. Otherwise it
  answers 200, with each concept's status.
- **This changes the cross-repo contract in two places:**
  - relays are reported per concept, in the Duty Menu's shape, where the ask had one top-level list of URLs;
  - bad input answers 422 (D1), where the ask said 400.

  Brainstorm-UI#299 updates `AssistantDictionaryResult` and the ask doc to match, and issue #112's text follows. The UI
  reads only `status` and `error` today.

### D7 — One press at a time per Assistant

`assistant_dictionary_service` keeps its own per-Assistant `asyncio.Lock`, the `_one_press_at_a_time` pattern, held
across a whole request. Add and withdraw share it, so two presses can't sign the same coordinate with the same
`created_at`. It's in-process, as the self-Map's is; move it into Redis if the server scales out.

## Consequences

- **What this enables:**
  - Brainstorm-UI's "Add to my Assistant's Dictionary" and Remove work. A 404 or 405 no longer comes back.
  - The Assistant publishes its first kind-39998 headers.
  - Once copies exist, the self-Map's Concepts line could stop reading `not-published-here`. That's out of scope.
- **What it constrains:** the copy rule now lives in Brainstorm-UI and here. Changing `COPIED_TAGS` means changing
  both, and AC11's parity fixture fails until they agree.
- **New debt and follow-ups:**
  - A copy changes only when the customer presses Add again; there's no upkeep.
  - The lock is in-process.
  - The admin route in Brainstorm-UI's `ADMIN-ASKS.md` can call the same service with an Observer parameter.
  - Treasure Map entries for copies (`["39998:<d>", <Assistant>, <relay>]`) aren't written.
- **Firmware reinstall required?** No.

## Implementation notes

**New `app/services/dictionary_copy_build.py`.** Pure, no I/O:

- `COPIED_TAGS`;
- `WITHDRAWN_ALT`;
- `parse_concept_coordinate(value) -> tuple[str, str] | None` (pubkey, d);
- `copy_tags(community_event, coordinate) -> list[list[str]]`;
- `withdrawn_tags(d) -> list[list[str]]`;
- `points_at(event, coordinate) -> bool`;
- `is_withdrawn(event) -> bool`;
- `add_outcome(composed, newest, coordinate)` and `withdraw_outcome(newest, coordinate)`, each returning a status;
- `next_created_at(now, newest) -> int`.

**New `app/services/dictionary_relay_reader.py`.** `fetch_newest_headers(relay, authors, ds) -> dict[tuple[str, str],
dict]`, strict, as in D3: the newest verified kind-39998 event per (author, `d`). It's kept out of
`designation_service` so #110's code is untouched.

**New `app/services/assistant_dictionary_service.py`:**

- `add_to_assistant_dictionary(db, user_pubkey, concepts)` and `withdraw_from_assistant_dictionary(db, user_pubkey,
  concepts)`;
- keys through `get_or_create_brainstorm_observer_nsec_by_pubkey_on_db`;
- the lock (D7);
- the four steps of D6, in order;
- publishing through `duty_menu_service.connect_targets` / `send_event` / `relay_report` and
  `duty_menu_build.publish_targets` / `relay_key`;
- the self-check (D5);
- the 40-second send deadline (D6).

**Schemas:**

- `app/schemas/request_body_schemas.py`: `AssistantDictionaryBody(concepts: list[str])`, with the 1–10 bound and a
  validator applying `dictionary_copy_build.parse_concept_coordinate`. A failure answers 422 through FastAPI's
  default handler, as every request schema here does.
- `app/schemas/schemas.py`: `AssistantConceptResult` and `AssistantDictionaryResult`.
- `app/schemas/request_response_schemas.py`: `AssistantDictionaryResponse(data=...)`.

**`app/routers/user/router.py`:** the two `@router.post` routes, after the Treasure Map's, with the same placement
comment.

**Docs:**

- `app/services/CLAUDE.md` gets rows for the three new modules. The `duty_menu_service` row's "shared with the
  Assistant's Treasure Map" becomes "shared with the Assistant's Treasure Map and its Dictionary".
- `app/routers/CLAUDE.md` gets the two routes in its `/user` table, beside `/assistantDutyMenu`, with the same "must
  stay on this router" note.
- `BIBLE.md` §4: the paragraph listing what the Assistant key signs on request gains the Dictionary copies, as the Duty
  Menu and the Treasure Map added theirs. Its statement that Concepts "aren't kept up to date yet, so they are never
  listed" stays true.
- `app/services/self_map_build.py`, docstring only. Lines 21–22 say "the Assistant publishes no Concepts yet". It now
  publishes copies on request, which nothing keeps up to date, so Concepts stay never live and the code is unchanged.

**Tests** (Phase 3, the Tester's lane):

- `tests/test_dictionary_copy_build.py`, pure. It includes AC11's fixtures: the header and expected tags from
  Brainstorm-UI's `lib/conceptCopy.test.ts`, for both `exactCopyTemplate` and `withdrawnTemplate`.
- `tests/test_assistant_dictionary.py`, routes with relays faked at the service's import site and real signing, as
  `test_assistant_duty_menu.py` does.

## Blast radius

**Changed:**

- `app/routers/user/router.py`: two routes added; no existing route changes.
- `app/schemas/request_body_schemas.py`, `app/schemas/schemas.py` and `app/schemas/request_response_schemas.py`:
  models added only.
- `app/services/CLAUDE.md` and `app/routers/CLAUDE.md`: rows added.
- `BIBLE.md` §4: the list of what the Assistant key signs on request gains the Dictionary copies.
- `app/services/self_map_build.py`: docstring only (lines 21–22), no code.

**New:** `app/services/dictionary_copy_build.py`, `app/services/dictionary_relay_reader.py`,
`app/services/assistant_dictionary_service.py`, and the two test files.

**Imported, unchanged:**

- `duty_menu_service` (`connect_targets`, `send_event`, `relay_report`);
- `duty_menu_build` (`publish_targets`, `relay_key`, `RelayTarget`);
- `assistant_profile_service.assistant_relay_list`;
- `repos/brainstorm_nsec.get_or_create_brainstorm_observer_nsec_by_pubkey_on_db`;
- `utils/client_tag`;
- `core/config.settings.dictionary_relay`.

**Not consumers:**

- `designation_service` (its reader isn't reused or changed);
- `self_map_service`, and `self_map_build`'s code (the self-Map's Concepts line is untouched; only that module's
  docstring changes, listed under Changed);
- `routers/setup` (the `/setup` rows are unchanged);
- the GrapeRank and TA publish path (`message_queue_tasks/`);
- every database model and migration, Neo4j, and Redis.

No setting is added.

**Outside this repo:** Brainstorm-UI#299 changes `AssistantDictionaryResult` (per-concept `relays`) and its ask doc
(D6).

## Out of scope

- The admin route (`POST /admin/dictionary/{observer}`).
- Copying items (curation copies).
- Editing the Assistant's copy.
- Upkeep when the community header changes.
- Treasure Map entries for copies, and the self-Map's Concepts line.
- A `GET` for the Assistant's copies: Brainstorm-UI reads them from relays.
- Moving the press lock into Redis.
