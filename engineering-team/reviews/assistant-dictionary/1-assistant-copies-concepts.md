# Review: Story 1 — Your Assistant's Dictionary: the Assistant signs concept copies

**Reviewer:** Claude (acting as Reviewer, independent of the authoring session)
**Date:** 2026-10-28
**Diff:** `git diff e2ca872 HEAD` (HEAD = `6c0b9f7`; base `e2ca872` is the head of #110, which this branch is stacked on)
**Story:** `engineering-team/stories/assistant-dictionary/1-assistant-copies-concepts.md`
**ADR:** `engineering-team/decisions/assistant-dictionary/0001-the-assistant-signs-concept-copies.md` (Accepted 2026-10-27)
**Test plan:** `engineering-team/stories/assistant-dictionary/1-assistant-copies-concepts.test-plan.md`
**Counterpart read:** Brainstorm-UI `feat/add-to-dictionary` at `cea36065` (PR #299): `docs/dictionary/ASSISTANT-COPY-ASK.md`,
`client/src/lib/conceptCopy.ts` and `conceptCopy.test.ts`, `client/src/components/dictionary/AddToDictionary.tsx`,
`client/src/services/api/users.ts`, `client/src/services/api/core.ts`, `client/src/services/dictionary.ts`,
`client/src/lib/dlistFields.ts`, `client/src/config/tagging.config.json`.
**Profile:** Light (trial), Gate B at full rigor. The story is security-adjacent (the server signs as a customer's
Assistant on their request), so the prose is full depth.

Branch commits audited: `afe4e07` story and intake → `1914e2a` ADR → `498fc5f` failing tests → `1e26204`, `c32c5b6`
test tightening (J2's notes) → `491819c` implementation → `6d2496e`, `6c0b9f7` doc fixes. The implementation commit
touches no test file (`git show --stat 491819c`), and `git diff c32c5b6 HEAD -- tests/` is empty: no test changed
after the test-design phase.

## Quality gates (run by reviewer, not trusted)

The story's gate is the deviation approved at Gate A: `poe check_all`'s stages with the integration tests deselected,
plus parity evidence for the 143 integration tests. Every stage ran from the worktree root with its `.venv` (Python
3.12.12; nostr-sdk 0.41.0, websockets 16.0, pydantic 2.13.1, FastAPI 0.128.8), in the foreground, with its exit code
captured by brace-redirect and never piped through `tail`.

| Stage | Command | Result |
| --- | --- | --- |
| black | `{ .venv/bin/black --check ./app; echo "black=$?"; }` | `black=0`, 195 files would be left unchanged |
| isort | `{ .venv/bin/isort --check ./app; echo "isort=$?"; }` | `isort=0` |
| flake8 | `{ .venv/bin/flake8 ./app --max-line-length 100 --extend-ignore E203; echo "flake8=$?"; }` | `flake8=0` |
| mypy | `{ .venv/bin/mypy ./app; echo "mypy=$?"; }` | `mypy=0`, "Success: no issues found in 195 source files" (plus the pre-existing `annotation-unchecked` notes in `nostr_event_transferer.py`) |
| Fast suite | `{ .venv/bin/pytest -m "not integration" -q -p no:cacheprovider ./tests; echo "pytest=$?"; }` | `pytest=0`: **1591 passed, 143 deselected, 8 warnings in 15.07s**. No skip or xfail marker in the output. The 8 warnings are in untouched suites (JWT key length; un-awaited `AsyncMock`s in `test_auth_challenge.py` and `test_billing_repo.py`). |
| The story's tests | `.venv/bin/pytest -q -p no:cacheprovider -rA tests/test_dictionary_copy_build.py tests/test_dictionary_relay_reader.py tests/test_assistant_dictionary.py` | 97 passed in 2.04s: 41 B (pure), 9 W (reader over a real socket), 47 R (routes) |
| Integration, HEAD | `{ .venv/bin/pytest -m integration -q -p no:cacheprovider -rfE ./tests; … }` | exit 1: **47 failed, 96 errors** (1591 deselected) in 180.25s |
| Integration, base `e2ca872` | The same command, run in an export of the base (`git archive e2ca872 \| tar -x` into a scratch directory, with the same venv; the worktree was not touched) | exit 1: **47 failed, 96 errors** (1494 deselected) in 205.92s |

**Parity holds as the story states it.** The sorted FAILED/ERROR node ids of the two runs are identical, 143 and 143,
and `diff` is empty. Every traceback in both outputs is a refused connection: Neo4j at `127.0.0.1:7687` (70 sections),
Postgres at `5432` (27), Redis at `6379` (8), and one `init_nostr_client` assertion on `try_connect`'s failures (no
local strfry). `git status` is clean after every run (mypy's cache is git-ignored).

## Diff walk (file by file)

- **`app/services/dictionary_copy_build.py`** (new, 132 lines, pure). `COPIED_TAGS` (38–51) is the UI's list item for
  item. `WITHDRAWN_ALT` (30–32) is the UI's sentence. `copy_tags` (67–73) gives `d` (first `d` with a value), every
  `COPIED_TAGS` tag verbatim in the header's order, the pointer `b` and the client tag. `withdrawn_tags` (76–78).
  `points_at` and `is_withdrawn` (89–98): the deferred `b` names nothing, and "withdrawn" means deferred `b`s only.
  `add_outcome` and `withdraw_outcome` (105–124) are D4's tables, with the client tag ignored in the `unchanged`
  comparison. `next_created_at` (127–132). The coordinate grammar is `^39998:([0-9a-f]{64}):(.+)$` used with
  `re.match` (55, 63). That grammar is Blocking finding 1.
- **`app/services/dictionary_relay_reader.py`** (new, 93 lines). `fetch_newest_headers` (51–93) opens one websocket,
  sends one REQ (`kinds`, `authors`, `#d`) and reads until EOSE, all under `asyncio.timeout(READ_TIMEOUT_SECONDS)`.
  The bound is read when the read starts (64), which is the seam W7 uses. A `CLOSED` raises `RuntimeError` (86–88).
  An early close raises from `ws.recv()`. Events are kept by `(pubkey, first d)`, filtered to the asked authors and
  `d`s, compared by NIP-01's rule (later `created_at`, then lower id; 42–48) and verified with nostr-sdk (28–32). The
  raw dict is stored only after it verifies. `_newer` runs before `_verified` (80–81), which is Non-blocking finding 3.
- **`app/services/assistant_dictionary_service.py`** (new, 320 lines). `_press` (134–152): the key comes from
  `get_or_create_brainstorm_observer_nsec_by_pubkey_on_db` (outside the lock), then everything else runs under
  `_one_press_at_a_time(assistant)`. `_named` (155–167) folds exact duplicates, keeping the first. `_read` (170–192)
  skips the community read for a withdraw, and when every author is the Assistant itself. It turns any reader exception
  into 502 `NOT_READ`. `_decide_and_sign` (195–240) runs, per concept: same `d` → `conflict`; own coordinate →
  `conflict`; otherwise D4, signing and `_check`ing each event that publishes. `_check` (263–280) checks `verify()`,
  the kind, the pubkey, a first tag equal to `["d", d]`, and exactly one `b`, equal to the expected one. `_send`
  (283–320) sends over one `Client` to `publish_targets([dictionary_relay], assistant_relay_list())`. Each send starts
  only while `loop.time() - started < SEND_DEADLINE_SECONDS`, and the client is disconnected in `finally`. A 502 is
  raised when every signed event ended `failed` (148–151).
- **`app/schemas/request_body_schemas.py`** (+17). `AssistantDictionaryBody.concepts: list[str] = Field(min_length=1,
  max_length=10)`, with an after-validator applying `parse_concept_coordinate` (45–58). It's the first schema to
  import from `app/services/`. The ADR authorized it and the story records it. There's no cycle: the module imports
  only `app.utils.client_tag`, and `app/services/__init__.py` is empty.
- **`app/schemas/schemas.py`** (+35). `AssistantConceptResult` (823) has a `status` Literal of the seven UI words and
  `relays` defaulting to `[]`. `AssistantDictionaryResult` (847).
- **`app/schemas/request_response_schemas.py`** (+5). `AssistantDictionaryResponse(SuccessfulResponseDataSchema)` (168).
- **`app/routers/user/router.py`** (+43). The two `@router.post` routes (392–426) sit on the authenticated `router`,
  after the Treasure Map's, with the placement comment. Each is one service call wrapped in the response. The router is
  mounted with `Depends(verify_token)` and before `public_router` (`app/routers/router.py`), and `verify_token` raises
  401.
- **Docs.** `app/services/CLAUDE.md` gains three rows, and the line counts check out (132, 93, 320, 893). The
  `duty_menu_service` row now reads "Treasure Map and its Dictionary". `app/routers/CLAUDE.md` gains two rows (132–133);
  the withdraw row omits `failed` (Non-blocking 6). The `BIBLE.md` §4 sentence is accurate. `self_map_build.py`'s
  change is docstring only. `engineering-team/` holds the story's own artifacts.

## Spec adherence: AC verdict table

Each verdict comes from reading the tests and the code, not from the green run alone.

| Criterion | Tests | Code | Verdict |
| --- | --- | --- | --- |
| **AC1** adding publishes an exact copy, signed by the Assistant, with a per-relay report | B1, B2, B8, R1 (also R16, R23 for the report) | `copy_tags` 67–73; `_sign` 254–260; `_send` 311–314 | **Met.** R1 checks the signed event's tags, pubkey, kind, `content == ""`, `verify()` and one line per relay. R1's fixture carries only `names`/`description`/`required`, and B1 pins the UI fixture on the *composed* list. My probe P2 signed the full UI fixture (`field-type`, `display`, `image`, `link`) through the route: the event's tags equal the UI expectation plus the client tag. See Non-blocking 2. |
| **AC2** adding again with nothing changed is `unchanged` and sends nothing | B10, B11, R2 | `add_outcome` 112–114 | **Met.** P2 also re-added the full UI fixture: `unchanged`. |
| **AC3** after the community header changes, an add is `updated` and republishes | B12, R3 | `add_outcome` 114; `next_created_at` | **Met on the server.** In the shipped UI the Add button disappears once the Assistant holds the concept, so the UI offers no way to reach this path (Non-blocking 7). |
| **AC4** never re-points; a second coordinate with the same `d` is a `conflict` | B14, R4, R5 (also R18) | `add_outcome` 115; `_decide_and_sign` 211–217, 232–234 | **Met.** Nothing is signed for a conflict. The same-`d` rule also refuses the second concept when the first was `not_found` (probe P4). That follows D4's "named earlier", not "signed earlier", and the UI sends one coordinate per call. |
| **AC5** withdraw: `withdrawn` / `unchanged` / `conflict` | B3, B15–B17, R6–R8, R22 | `withdraw_outcome` 118–124; `withdrawn_tags` | **Met.** R6 checks the exact withdrawn tags on the signed event. R22: a copy whose community header is gone can still be withdrawn. |
| **AC6** adding after a withdrawal publishes a fresh copy | B9, R9 | `add_outcome` 110–111 | **Met.** |
| **AC7** unknown concept → `not_found`, nothing published | R10, R19 | `_decide_and_sign` 224–227 | **Met.** `local` is null, as the UI's type says. |
| **AC8** input: 422 for anything that isn't 1–10 kind-39998 coordinates; a `d` with colons kept whole | B4, B5, R11 (7 bodies × 2 routes), R12 | `_COORDINATE` 55, `parse_concept_coordinate` 61–64; `AssistantDictionaryBody` 45–58 | **Not met (Blocking 1).** Colons are kept whole (B4, R12). But a coordinate ending in one `\n` is accepted (200), split as if the `d` had no newline, and its raw string is signed as the copy's `b`. A `d` with an inner newline is refused, though D1 lets a `d` hold anything after the second colon. |
| **AC9** neither path is answered by `GET /user/{pubkey}`; both refuse a request without a session | R1 (the new route answers 200, not `/{pubkey}`'s 405), R6 (`/withdraw` answers 200, not 404), R13 (401) | `router.py` 392–426; mounting order in `routers/router.py` | **Met.** |
| **AC10** published to the dictionary relay and the outbox, dated `max(now, newest + 1)`; 502 when no relay took it | B18, R14, R15, R24 | `_send` 290; `next_created_at`; `_press` 148–151 | **Met as written.** The "any relay" success rule sits badly with D3's "the dictionary relay decides" (Non-blocking 1). |
| **AC11** the server's copy and withdrawn copy have the UI tests' tags, plus the client tag | B1, B3 | `COPIED_TAGS`, `copy_tags`, `withdrawn_tags` | **Met.** I compared B1 and B3 with `conceptCopy.test.ts` (`describe("exactCopyTemplate")` 202–247, `describe("withdrawnTemplate")` 249–281) and `conceptCopy.ts` (`COPIED_TAGS` 263–276, `exactCopyTemplate` 283–291, `withdrawnTemplate` 294–305): the same list, order and sentence. The UI's `header()` helper prepends the `d`, as the server fixture does. P2 extends this to the signed event. |

No criterion was dropped silently. The behaviour added beyond the letter of the ADR is all recorded in the story's
"Implementation scope notes": one read for a withdraw, the self-authored rule for a withdraw, and a schema importing a
pure service module. All three sit inside the ADR's intent.

## Evidence table

| Claim | Evidence | Result |
| --- | --- | --- |
| The gate is green under the approved deviation | The five stage runs above | All exit 0; 1591 passed |
| Integration parity with the base | Both runs, sorted node ids, `diff` | Identical 143; all refused connections |
| No test was weakened after the red phase | `git show --stat 491819c`; `git diff c32c5b6 HEAD -- tests/` | The impl commit touches no test; the diff is empty |
| The tests failed before the implementation | The test plan's Verification (assertion failures for R11/R13, `ModuleNotFoundError` elsewhere) | Consistent with the commit order; not re-run at `1914e2a` |
| nostr-sdk keeps copied tags verbatim when signing | Probe `probe_pure.py`: 19 tag shapes through `Tag.parse` → `sign_with_keys` → `as_json` (URLs with and without a path, mixed-case URLs, a non-URL `image`, one-element tags, empty strings, unicode, repeats) | All identical, with order and repeats kept (nostr-sdk 0.41.0 is pinned exactly in `pyproject.toml`) |
| The full UI fixture survives the route | Probe P2: the UI's community header signed by a curator, through `POST /user/assistantDictionary` | Signed tags == UI expectation + `["client","Brainstorm"]`; a second add is `unchanged` |
| A trailing newline is accepted and signed | Probe P1, through the route with the story's own fakes | 200 `published`; `b` = `"39998:<pk>:github-accounts\n"`, `d` = `github-accounts`; then adding and withdrawing the real coordinate both answer `conflict` |
| What a 422 looks like to the UI | Probe P3; `node` with `extractApiError`'s logic | `detail` is pydantic's list, so the UI's `new Error(detail)` reads "[object Object]" |
| The strict reader works against the real dictionary relay | NIP-11 of `wss://dcosl.brainstorm.world` (strfry 1.0.4; kinds 9998/9999/39998/39999/7; `max_message_length` 131072; no `auth_required`/`restricted_writes`). An anonymous REQ, then `fetch_newest_headers` for 6 real headers. Copies composed and signed locally with a throwaway key, **never sent** | EOSE without auth; 6 verified headers returned; every composed copy signs verbatim; a re-add of each is `unchanged` |
| The UI and the server read copies from the same relay | `tagging.config.json` `tagRelays` = `["wss://dcosl.brainstorm.world"]`; `settings.dictionary_relay` default | Same relay |
| A malformed relay event can fail a read | Probes RP1 and RP2 (story's one-shot local relay) | A valid event followed by an unverifiable one with `created_at: "soon"` raises `ValueError` from `_newer`. A non-JSON frame raises `JSONDecodeError`. Both become 502 (fail-closed). |
| No new dependency | `git diff --stat e2ca872 HEAD -- pyproject.toml poetry.lock` | Empty. `websockets ^16.0` and `nostr-sdk 0.41.0` were already direct dependencies. |
| The time budget fits the UI's wait | `CONNECT_TIMEOUT` 4 s and `SEND_TIMEOUT_SECONDS` 12 (`duty_menu_service.py:65,68`); `READ_TIMEOUT_SECONDS` 5.0; `SEND_DEADLINE_SECONDS` 40.0; UI `AbortSignal.timeout(60000)` (`users.ts:173`) | At most about 52 s after the lock is taken, plus the key lookup. Fits. |
| Copies reaching the TA relay feed nothing back | `app/message_queue_tasks/CLAUDE.md` row for `strfry:events` | Kind 39998 isn't dispatched ("Other kinds … are ignored"). Trusted Lists consume 39999 only. |
| Documented line counts | `wc -l` | 132 / 93 / 320 / 893, as documented |
| No debug leftovers | grep for `print(`, `breakpoint(`, `pdb`, `TODO`, `FIXME`, `XXX` over the three new modules | None |

## ADR adherence

- [x] **Files match the implementation notes and the blast radius.** The diff adds the three new modules and two
  routes, and only adds models to the three schema files. It adds rows to the two `CLAUDE.md`s, the BIBLE §4 sentence,
  and the `self_map_build.py` docstring. The one file outside the ADR's letter, `tests/test_dictionary_relay_reader.py`,
  is named and justified in the story's scope notes. It was committed in the test phase, and the test plan's coverage
  map and error-path table claim it. `duty_menu_service.py`, `duty_menu_build.py`, `designation_service`,
  `self_map_service.py`, `routers/setup` and `config.py` are unchanged. No setting is added.
- [x] **Decisions.**
  - D1: two POST routes on `router`; the 1–10 body; 422 from the schema; duplicates folded after validation.
  - D2: the tags in order, `content ""`, no `alt` on an add, the withdrawn form.
  - D3: one strict reader with a 5 s bound and verified signatures, matched by exact (author, `d`), on the dictionary
    relay only (R25). A withdraw reads once, as the story records.
  - D4: both tables, plus the four cases that come before them.
  - D5: the targets, the date, and the self-check before any send.
  - D6: read, sign, then send; the 40 s deadline; the per-concept answer; 502 only when everything that needed sending
    failed.
  - D7: one in-process lock per Assistant, shared by add and withdraw (R21).
- [x] **Layering.** Router → service → pure builder, reader, repos. The Duty Menu's relay I/O is imported unchanged.
  The schema → service import is ADR-authorized. It could live in `app/utils/` (as `shorturl_entities` holds the
  share-link grammar), but that's a preference, not a finding.
- [x] **No unauthorized dependencies** (see the evidence table).

## Gate A classification: ratified

The story is classified **ADR**, and that's right. I checked the Gate A triggers one by one:

| Trigger | Present? | Where |
| --- | --- | --- |
| A wire format or event shape | **Yes** | The Assistant's kind-39998 copy and its withdrawn form; the response shape |
| An auth/trust default | **Yes** | What the Assistant key signs, at whose request (Option C was rejected for this reason) |
| A schema or migration | No | No DB model or migration; API models only (covered by the contract trigger) |
| A new dependency | No | `pyproject.toml` and `poetry.lock` unchanged |
| A cross-repo contract | **Yes** | Brainstorm-UI#299's `AssistantDictionaryResult`, its ask doc, the 422 and the per-concept `relays` |
| Request routing or middleware ordering | **Yes** | Placement on `router` ahead of `public_router`'s `/{pubkey}` |
| Response headers or content-type | No | JSON default, no header changes |
| A value in more than one repo | **Yes** | `COPIED_TAGS`, the withdrawn `alt`, the status words, the error statuses |

No trigger was missed. Standard phases ran: the ADR has Options considered (A/B/C), Decision, Consequences and Blast
radius. One bookkeeping wrinkle is recorded under Harness friction 1.

## Repo-conventions integrity

*(This replaces tapestry's "Concept-graph integrity", as the shortest-path review did: this repo has no concept graph.
Kind-39998 coordinates do appear here, in the `kind:pubkey:d` form the template's handle check names.)*

- [x] **Everything is async.** The routes, service, reader and relay I/O are all `async`. The only sync work is
  signing, verifying and JSON (CPU-bound and small).
- [x] **Logging goes through `loggr`.** No `print`, no stdlib `logging`.
- [x] **The client tag is on every signed event** (`copy_tags` 73, `withdrawn_tags` 78; checked by R1 and R6).
- [x] **Signing uses the per-Observer Assistant key** (BIBLE §4's invariant), through the same repo call as the Duty
  Menu. The key is `ta_pubkey`'s, the Assistant the UI names.
- [x] **The response wrapper.** `AssistantDictionaryResponse(SuccessfulResponseDataSchema)`. The UI reads
  `json?.data ?? json`.
- [x] **No Neo4j, Postgres write or Redis path,** apart from the existing get-or-create key lookup (it can create the
  key row; `get_db` commits on success and rolls back on an `HTTPException`).

## Adversarial probes (beyond the test plan)

**Premise errors.**

- *The UI's 422 handling (D1, ADR lines 105–106).* D1 says Brainstorm-UI "reads 422 as it would have read 400". It
  doesn't. A 400 from `HTTPException` carries a string `detail`, which the UI would show. A 422's `detail` is
  pydantic's list, and `extractApiError` (`core.ts:265–268`) hands that list to `new Error(…)` (`users.ts:160`), so the
  UI shows "[object Object]". The UI only sends coordinates it built itself, so this is close to unreachable from the
  product today (Non-blocking 5).
- *Option A's protection (ADR line 62).* Option A's pro says "Nothing a client sends becomes signed tags". Two things
  weaken that. First, the coordinate string the client sends *is* signed, verbatim, as the `b` value; that's how
  Blocking 1 reaches the wire. Second, the content bound is "any header any key has put on the dictionary relay", and
  dcosl is open for writes (NIP-11 declares only a kind policy). A caller can publish a header under a throwaway key
  and have their Assistant copy its 12 definition tags verbatim. The decision stands: Option A still yields one exact
  copy for every client, and the Assistant is the caller's own (UI ADR 0009). But the Consequences should say plainly
  what the Assistant will sign (Non-blocking 4).
- *"The dictionary relay decides" against "success is any relay" (D3 against D6/AC10).* See Non-blocking 1.
- *The live relay.* The premises that the strict reader works against `wss://dcosl.brainstorm.world` without NIP-42
  auth, that real headers verify, and that their copies sign verbatim all held when checked read-only against
  production (evidence table).

**What a malicious or confused caller can make the Assistant sign and publish.** A session is bound to its own
Assistant, so a caller can only drive that one key. It can make the Assistant:

- sign a kind-39998 at any `d` that holds no conflicting header, carrying the 12 `COPIED_TAGS` of any header on
  dcosl, plus `b` = **the coordinate exactly as sent** (Blocking 1) and the client tag;
- republish its own copies as withdrawn;
- do either for up to 10 concepts per request, with no rate limit, to dcosl, the TA relay, the trusted-list relay
  (when `trusted_list_relay` is set) and four public relays (`relay.damus.io`, `relay.primal.net`, `nos.lol`,
  `relay.nostr.band`). Those events are attributed to an identity with a NIP-05 under this server's domain
  (Non-blocking 8).

It cannot:

- change the kind, set `content` or add any other tag name;
- re-point an existing copy (AC4) or overwrite a non-copy;
- make the Assistant copy its own coordinate (R18);
- sign as another customer's Assistant.

**What a malicious or broken relay can do.**

- The dictionary relay can't forge a header: `verify()` recomputes the id as well as the signature, which is what W3
  pins.
- It can withhold the newest version, so the Assistant copies a stale definition or decides from stale state. That is
  inherent in D3's single source.
- It can stall a read for at most 5 s.
- It can fail a read by closing, sending `CLOSED`, sending non-JSON or sending a malformed event. Every one of these
  fails closed as a 502 with nothing signed (RP1, RP2, R17, W5–W8).
- A publish relay can refuse; that shows as `failed` or a 502.

**Error paths.**

- The key lookup raising gives the app's generic 500, as the test plan declares.
- A reader exception of any type gives 502 with nothing signed (the `except Exception` at `_read` 187).
- A failed self-check gives 500 with nothing sent, since every check finishes before the first send.
- `send_event` swallows every `Exception` itself (`duty_menu_service.py:138–145`), so no partial send can surface as a
  500. Probe P5 forced a raise past it: the earlier event had already gone out and the request errored. This is only
  reachable through a `BaseException` such as cancellation, and D6 accepts it ("a reload shows what was published").

**Concurrency.**

- R21 shows the lock serializing two presses, with one send in flight.
- The lock is in-process, as the self-Map's is. `start.sh` runs one uvicorn worker, and scaling out is recorded debt.
- `_presses` grows by one `Lock` per Assistant ever pressed, the same as `self_map_service`.
- There's one residual tie. Suppose press 1's event reached only the outbox, and press 2 runs within the same second.
  Press 2 reads an older version from dcosl and dates its event `now`, the same `created_at` as press 1's. The outbox
  then keeps whichever has the lower id. It needs a dcosl miss inside one second (Non-blocking 1's family).

**Input validation at the boundary.**

- The body is strictly a list of 1–10 strings, each matching the grammar, so the shape is sound.
- The grammar itself isn't total over the string (Blocking 1).
- Values reach the REQ through `json.dumps`, so nothing can be injected.
- A `d` of any length only makes the relay refuse or answer empty, which ends in 502 or `not_found`.

**Collateral damage outside the diff.**

- The Duty Menu's and the Treasure Map's relay I/O is reused byte for byte. `tests/test_assistant_duty_menu.py` and
  `tests/test_assistant_treasure_map.py` stay green (sentinel S1).
- Copies land on the TA relay, but kind 39998 isn't dispatched by `process_strfry_event`, so nothing feeds back into
  Trusted Lists (39999).
- The self-Map still lists Concepts as never live. That's correct, since nothing keeps a copy up to date, and its code
  is unchanged.
- `request_body_schemas` now imports `app.services.dictionary_copy_build`, whose only import is the
  worker-safe `client_tag`.

**Does the server do what the UI expects?**

| Expectation (Brainstorm-UI) | Server | Match |
| --- | --- | --- |
| `POST /user/assistantDictionary` and `/withdraw`, body `{concepts: [coordinate]}`, signed-in | Two routes under `verify_token` | ✓ |
| `data.concepts[0].status` in the UI's 7-word union; `error` a sentence | The `AssistantConceptResult.status` Literal; sentences for `conflict`, `not_found`, `failed` | ✓ |
| Per-concept `relays: [{url, ok, message}]` (#299's updated type) | `relay_report` lines | ✓ |
| The copy has `exactCopyTemplate`'s tags; the withdrawn copy has `withdrawnTemplate`'s, plus `["client","Brainstorm"]` (the tag the UI's `signAs` stamps) | B1, B3, R1, R6, P2 | ✓ |
| Copies are read from dcosl (`tagRelays`) | Reads from and decides on `settings.dictionary_relay` (dcosl) | ✓ |
| Within 60 s | At most about 52 s after the lock | ✓ |
| A non-2xx shows its message | 502 has a string `detail` (✓); 422 shows "[object Object]" | ✗, on the UI side (Non-blocking 5) |
| `published`/`withdrawn` means the change is now visible on dcosl | It means *some* relay took it | ✗ in a dcosl-refusal case (Non-blocking 1) |
| The UI can only send the canonical `coordinateOf` string | The server accepts a trailing-newline variant and signs it | Server-side defect (Blocking 1) |

## Things tests can't catch

- [x] **No secrets.** The test keys are generated, and the nsec is read through the existing repo function.
- [x] **No debug logging or `print`.**
- [x] **No commented-out code, TODO or FIXME.**
- [ ] **Error paths and edge cases are handled where it matters.** Blocking 1 is an unhandled edge at the boundary
  that matters most here. Everything else is handled (see the probes above).
- [x] **Concurrency considered.** The lock, its in-process scope and the residual tie are covered above.
- [ ] **Security: input validation at the boundary.** Blocking 1.

## House rules check

- [x] No new lint, typecheck or build tooling; no config or hook changes.
- [x] The cross-repo constant is copied verbatim and named at its source (`dictionary_copy_build.py:34–37`), as the
  Trusted Lists ADR 0001 D8 precedent does.
- [x] Product copy: the server's sentences are written for the customer and are shown verbatim under the UI's line.
  This story traces to a UI ADR, not a PRD, so the style-guide check doesn't apply.

## Findings

### Blocking

1. **`app/services/dictionary_copy_build.py:55,63`: a coordinate with one trailing newline is accepted, split
   wrongly, and its raw string is signed as the copy's `b`.**
   - **Why it happens.** `_COORDINATE` is `^39998:([0-9a-f]{64}):(.+)$`, used with `re.match`. In Python, `$` also
     matches before a final `\n`, and `.` doesn't match `\n`. So `parse_concept_coordinate("39998:<pk>:x\n")` returns
     `(pk, "x")`: the schema (`request_body_schemas.py:52–58`) passes it, and `_named`
     (`assistant_dictionary_service.py:166`) keeps the raw string.
   - **What the server then does.** It reads and copies the header at `(pk, "x")`, but `copy_tags`
     (`assistant_dictionary_service.py:228`) signs `["b", "39998:<pk>:x\n", "pointer"]`. `_check` (238) passes,
     because it compares the `b` with the same raw string. The Assistant then broadcasts a header at `d = x` whose
     pointer names a coordinate that isn't the one it copied. That breaks the story's core invariant (AC1/D2: an exact
     copy pointing back at its community header) and AC8/D1's grammar.
   - **Reproduced end to end** (probe P1, the story's own fakes): 200 `published`, with the `b` above. Afterwards,
     adding the real coordinate answers `conflict` and withdrawing it answers `conflict` too. The UI can only send the
     canonical `coordinateOf` string, so the customer can't repair that `d` from the product. Only another call
     carrying the same malformed string undoes it.
   - **The same regex refuses a `d` with an inner newline** (422), although D1 lets a `d` hold anything after the
     second colon and the UI's `coordinateOf` builds coordinates from the raw `d`.
   - **Reachability.** It's reachable by any client other than the shipped UI. The ADR names such clients as
     consumers: "a script, a future admin route". A script that reads coordinates one per line from a file without
     stripping them would trigger it for every concept, and every concept would answer `published`. No test covers
     it: B5 has a leading space but no trailing newline, and R11 has neither.
   - **Asked change.** Make the grammar total over the string, so that every accepted coordinate is exactly
     `f"39998:{pubkey}:{d}"`. For example: `_COORDINATE = re.compile(r"39998:([0-9a-f]{64}):(.+)", re.DOTALL)` with
     `_COORDINATE.fullmatch(value)`, which keeps any non-empty `d` whole, newlines included. If control characters
     should be refused instead, refuse them explicitly with a 422. Either way, add the trailing-newline coordinate to
     B4 (expecting `(AVI, "x\n")`) or to B5 (expecting `None`). Add the matching route case too: R11's 422 bodies, or
     an R12-style test asserting the signed `d` and that `b` equals the coordinate.
   - **Optional hardening.** Have `_check` compare the signed `b` with the coordinate rebuilt from `(author, d)`
     rather than with the raw input, so a future grammar regression can't pass the self-check.

### Non-blocking

1. **`app/services/assistant_dictionary_service.py:311–318` and `:148–151`: success means "any relay took it", while
   D3 makes the dictionary relay the only one that decides, and the UI reads copies only there.**
   - **What happens.** If dcosl refuses or drops a copy that the outbox accepts, the concept answers `published` (or
     `withdrawn`), and the UI counts that as done (`AddToDictionary.tsx` `DONE`).
   - **What the customer sees.** The UI refreshes from dcosl and shows no change, with no error, since the UI doesn't
     read `relays`. The next press decides from dcosl again and republishes, so it heals on retry. But the failure is
     silent on the one relay that matters, and that's also when the residual same-second `created_at` tie can happen.
   - **Why it isn't blocking.** AC10, approved at Gate A, specifies the any-relay rule, mirroring the Duty Menu (which
     has no single deciding relay).
   - **Suggested follow-up.** When the dictionary relay didn't take a concept's event, answer `failed` for it, with a
     sentence such as "Your Assistant's copy didn't reach the Dictionary relay; try again", even if other relays took
     it. Amend AC10 and D6, and tell #299 (the UI already shows `error` for `failed`).
2. **`tests/test_assistant_dictionary.py:59–78` and `app/services/assistant_dictionary_service.py:270–276`: AC11 is
   pinned only on the composed tags, and the self-check doesn't compare them.**
   - B1 pins `copy_tags`. R1's fixture has no `field-type`, `display`, `image` or `link`. `_check` checks only `d`
     and `b`.
   - nostr-sdk 0.41.0 keeps tags verbatim (P2 and the 19-shape probe). An upgrade that normalized a tag would still
     pass every test, put a non-exact copy on the wire, and turn every later add from `unchanged` into a perpetual
     `updated`.
   - **Optional improvement.** Sign the UI fixture through the route in a test (P2 is one), and/or have `_check`
     assert `signed["tags"] == tags`, which makes "exact copy" a runtime invariant.
3. **`app/services/dictionary_relay_reader.py:47,80–81`: `_newer` runs on unverified data.**
   - An unverifiable event with a non-numeric `created_at`, arriving after a valid one for the same (author, `d`),
     raises `ValueError` before `_verified` can skip it. The whole request then answers 502 (RP1).
   - It fails closed, and strfry validates events on ingest, so dcosl won't serve one.
   - **Optional improvement.** Verify first (the cost is one schnorr check per matching event), or make `_newer`
     tolerant of a non-integer `created_at`.
4. **`engineering-team/decisions/assistant-dictionary/0001-the-assistant-signs-concept-copies.md:62` (and BIBLE.md §4):
   the trust consequence is understated.**
   - The Assistant will sign the 12 definition tags of any header any key has put on the dictionary relay, including
     one the caller just authored under a throwaway key.
   - It broadcasts that copy to public relays under an identity with a NIP-05 at this server's domain.
   - **Optional improvement.** Say this in the ADR's Consequences, next to "A header found under another author's
     coordinate is copied as named", so a later admin route or policy change starts from an accurate picture.
5. **ADR D1, lines 105–106 (UI side: `client/src/services/api/core.ts:265–268`, `users.ts:160`): a 422 shows
   "[object Object]".**
   - D1's claim that the UI reads 422 as it would have read 400 doesn't hold, because pydantic's `detail` is a list.
   - **Optional improvement.** On the UI side, have `extractApiError` turn a pydantic list into its first `msg`. On
     this side, correct the sentence in D1.
6. **`app/routers/CLAUDE.md:133`: the withdraw row lists `withdrawn`, `unchanged` or `conflict`.**
   - A withdraw can also answer `failed` (no relay took it, or out of time) and 502, like an add.
   - **Optional improvement.** Add `failed` and the 502 conditions to the row.
7. **Cross-repo, informational: AC3's `updated` path has no affordance in the shipped UI.**
   - `AddToDictionary.tsx`'s `Line` shows the held chip and Remove instead of Add once the Assistant holds the
     concept. The ask doc's "presses Add again, which answers `updated`" therefore needs Remove then Add, which answers
     `withdrawn` then `published`.
   - The server is correct. Upkeep is out of scope; this is for #299 or the upkeep story.
8. **`app/routers/user/router.py:392–426`: no rate limit on the two signing routes.**
   - Each request can publish up to 10 Assistant-signed events to up to 7 relays, four of them public, and alternating
     add and withdraw forces a publish every time.
   - This is the same pattern as the unlimited Duty Menu and Treasure Map POSTs, but with a 10× multiplier. Every
     customer shares the server's egress reputation with those public relays.
   - **Suggested follow-up.** A per-caller limit, the way `rate_limit_graperank` guards `POST /user/graperank` and
     `POST /user/followList` (`router.py:121,158`).

### Harness friction

*(Each becomes an `engineering-team/OPEN.md` row, type `meta`.)*

1. **Two records of the Gate A classification disagree.** `stories/_intake.md`'s 2026-10-27 entry names three triggers
   (an event shape, a cross-repo contract, request routing). The story names five, adding the auth/trust default and
   the value held in more than one repo. Gate A's record should be one list.
2. **The parity recipe isn't recorded.** The story says the base's 143 were "compared line for line", but not how the
   base run was produced. A reviewer re-running it needs a recipe that doesn't touch the worktree. This one worked:
   `git archive <base> | tar -x -C <scratch>`, then the worktree's `.venv/bin/pytest -m integration -p no:cacheprovider`
   in that directory (`pythonpath = ["."]` picks up the export's `app`), then sort and `diff` the FAILED/ERROR ids.
   Worth adding to the light profile's deviation precedent.
3. **The template's close-out doesn't fit Light's human-verdict Gate B.** Tapestry's checklist ("On PASS (same
   commit)": flip `Status`, commit the review) assumes the Reviewer commits and closes. Under Light's Gate B the
   operator gives the verdict, and this run's instructions keep the Reviewer from committing or touching the Status
   line. The light profile should say which steps move to the operator.
4. **The template needs re-adapting every time** (known since shortest-path). Its npm gate lines and Concept-graph
   section are tapestry's, and each review here re-adapts them by hand. A repo-local review template, or a note in
   `light-profile.md`, would stop the drift.

## Verdict

**CHANGES_REQUESTED**

One blocking issue: the coordinate grammar lets a request make the Assistant sign and broadcast a copy whose `b` isn't
the coordinate of the header it copied, and the UI can't repair it afterwards (Blocking 1). The fix is a one-line
grammar change plus its tests. Everything else is sound:

- The gate is green under the approved deviation, with integration parity reproduced against the base.
- Every other criterion is met, on evidence from both the tests and the code, and AC11 holds on the signed event as
  well as on the composed tags.
- The ADR is followed file for file and decision for decision.
- The strict reader fails closed on every relay failure I could provoke.
- The Gate A classification is ratified.

The non-blocking items are follow-ups. The first, success judged by the deciding relay, is the one most worth taking
up next.

**Re-review scope after the fix:**

- the grammar change in `dictionary_copy_build.py`;
- the new B4/B5 and route cases (and the optional `_check` hardening, if taken);
- a rerun of the five gate stages.

Nothing else needs re-reading unless the fix spreads beyond those files.

## On PASS

Not applicable to this verdict. Under the Light profile's Gate B, the operator gives the verdict. Flipping the story's
`**Status:**` line and committing this review are left to the operator, as this run's instructions require; this file
is uncommitted.

## Re-review (2026-10-28)

**Reviewer:** Claude (acting as Reviewer, independent of the authoring session and of the fix)
**Fix:** `e9adf7c` ("fix: a coordinate is parsed whole"), on top of `6c0b9f7` and this review's commit `f2434f3`. In code
and tests (`git diff 6c0b9f7 e9adf7c -- app tests`) it touches six files: the grammar in `dictionary_copy_build.py`, the
check order in `dictionary_relay_reader.py`, the withdraw row in `app/routers/CLAUDE.md`, and three test files. The
test files only add lines, apart from B4's rewritten comment.
**Read:** the fix commit, the story's "Changes from the Gate B review", the test plan's "Added at the Gate B review",
ADR D1 and the profile's Gate B section. The fix reaches past the re-review scope set above (the reader, the routers
doc, the ADR), so I re-read those as well.
**Environment:** the worktree's `.venv`: Python 3.12.12, nostr-sdk 0.41.0, websockets 16.0, pydantic 2.13.1, FastAPI
0.128.8, Starlette 0.52.1.
**Method:**

- **The pre-fix code** is a `git archive 6c0b9f7 | tar -x` export in a scratch directory, with HEAD's three test files
  copied in.
- **The probes** ran in a second export, at `e9adf7c`. The worktree ran only the gate, and `git status` stayed clean.
- **The probe sources** (`probe_grammar.py`, `test_rereview_*.py`) are in this session's scratchpad, uncommitted.

### Quality gates (run by reviewer, not trusted)

Each stage ran from the worktree root in the foreground, with its exit code captured by brace-redirect and never piped
through `tail`.

| Stage | Result |
| --- | --- |
| black | `black=0`, 195 files would be left unchanged |
| isort | `isort=0` |
| flake8 | `flake8=0` |
| mypy | `mypy=0`, "Success: no issues found in 195 source files" (with the same `annotation-unchecked` notes in `nostr_event_transferer.py`) |
| Fast suite | `pytest=0`: **1596 passed, 143 deselected, 8 warnings in 15.19s**. That's the earlier 1,591 plus B4's two new cases, R26, R27 and W9. No skip or xfail. The same 8 warnings, all in untouched suites. |
| Integration, HEAD | exit 1: **47 failed, 96 errors** (1596 deselected) in 266.78s |
| Integration, the `6c0b9f7` export | exit 1: **47 failed, 96 errors** in 238.44s |

**Parity holds.**

- The sorted FAILED/ERROR node ids of the two runs are identical, 143 and 143, all under `tests/integration/`.
- Every error is a refused connection to Neo4j (7687), Postgres (5432) or Redis (6379), plus the one
  `init_nostr_client` assertion on `try_connect`.
- The earlier review showed `6c0b9f7` at parity with the base `e2ca872`, so HEAD is at parity with the base too.

### Blocking 1: resolved

`_COORDINATE` is now `39998:([0-9a-f]{64}):(.+)` with `re.DOTALL`, applied with `fullmatch`
(`dictionary_copy_build.py:58,66`). A full match of a literal, a fixed 64-character group, a literal and the remainder
makes the input exactly their concatenation. So an accepted coordinate always equals `39998:<pubkey>:<d>` as parsed. I
checked that by probe rather than take it from the argument.

**The grammar.** Probe G loads the pre-fix parser and the fixed one side by side.

- **A generated corpus of 60,000 values,** biased to the grammar's edges.
  - The alphabet: hex, colons, `\n`, `\r`, `\t`, `\v`, `\f`, NUL, `\x1c`–`\x1f`, NEL, U+2028/2029, ZWSP, BOM, an RTL
    override, a combining mark, lone surrogates, an emoji, and fullwidth, Cyrillic and Arabic-Indic lookalikes. The
    pubkeys have 63, 64 or 65 hex digits.
  - All 35,443 values the fixed parser accepts round-trip exactly.
  - The two parsers disagree on 2,774, and every one contains `\n`, so the fix changes nothing for a value without a
    newline.
  - The pre-fix parser also mis-split `…:x\r\n`, to `d` = `x\r`.
- **39 named edges.** Anything after the second colon is kept whole. These are refused:
  - an empty `d`;
  - a newline before the second colon, or after the kind;
  - a leading newline, space or BOM;
  - uppercase hex, or 63 or 65 hex digits;
  - lookalikes in the pubkey, fullwidth digits in the kind, or a fullwidth colon;
  - `039998` and `399980` as the kind.
- **Long input stays linear.** A 10 MB `d`, or 10 MB of newlines, parses in under 1 ms.

**The schema and the service agree.**

- On all 60,000 values, `AssistantDictionaryBody` accepts exactly what `parse_concept_coordinate` accepts, and hands
  the string on unaltered (`type is str`, nothing stripped).
- Both routes pass `body.concepts` straight to the service. Its `_named` (`assistant_dictionary_service.py:155–167`)
  calls the same function.
- Non-string elements (a number, `null`, `true`, a list, an object) are still a 422 on both routes, before any read
  (P10).

**What gets signed** (the story's fakes, real signing):

- **P6.** With headers held at both `x\n` and `x`, adding both publishes two copies.
  - Each copy's `d` is its header's, its only `b` is `39998:<header pubkey>:<header d>`, and it verifies.
  - Re-adding both answers `unchanged`.
  - Withdrawing `x\n` leaves the copy at `x` alone.
- **P7** takes 15 more `d`s through add, re-add and withdraw, each kept exactly: an inner newline, a lone one, CRLF, CR,
  tab, NUL, `\x1f`, NEL, U+2028, a combining mark, an RTL override, an emoji, a space, `::` and 5,000 characters.
- **P9** does the same through raw JSON escapes.
- **P12** puts 300 random `d`s through the real `_named` and `_decide_and_sign`, as an add and then a withdraw.
  - The signed `b` is always `39998:<header pubkey>:<header d>`.
  - The withdraw signs only the deferred `b`.
- **Against `6c0b9f7`,** exactly the 8 probe tests that use a `d` with `\n` fail. In P8, `…:x` and `…:x\n` in one
  request used to collide as one `d`; now they're two concepts.

The one way I found to make the signed `b` name an address other than the copied header's predates the fix, and doesn't
go through the grammar. It's a header whose first `d` tag has no value (Non-blocking 2).

**The author's tests.** Against the pre-fix code exactly four fail, each for the defect's own reason:

- B4 `…:github-accounts\n`: `'github-accounts' != 'github-accounts\n'`;
- B4 `…:a\nb`: the parser returns `None`;
- W9: `ValueError: invalid literal for int() with base 10: 'soon'` at `dictionary_relay_reader.py:47`;
- R26: `'published' == 'not_found'`.

R27 passes on both trees, as the test plan says. At HEAD all 102 pass (43 B, 10 W, 49 R).

- B4's new round-trip assertion strengthens its two old cases.
- R26 is the route case the review asked for, in a third shape: a negative case, showing the newline is no longer
  dropped. A positive one like P6 would pin the exact copy for such a `d` through the route too. That's optional.
- The optional `_check` hardening wasn't taken. It's moot while the grammar is total, but would still be cheap defence
  in depth.

### The non-blocking fixes

- **The reader's check order** (`dictionary_relay_reader.py:82–83`) is correct.
  - `_verified` and `_newer` are pure, so swapping them changes nothing when neither raises. These pass on both trees,
    and so do W1–W8:
    - RD1: keys are exact, including the `d`s `x` and `x\n`;
    - RD2: NIP-01's tie rule (same `created_at`, lower id wins) holds in either order;
    - RD3: a forged, later event after a valid one is skipped.
  - A malformed `created_at` is now skipped: W9's string, and a list in RD4. Some other malformed shapes still fail the
    read (Non-blocking 4).
- **R27 is Brainstorm-UI's fixture.**
  - I extracted its lists from the test's source. With only the coordinate substituted, its header equals B1's
    `UI_COMMUNITY_TAGS`, and its expectation equals `UI_EXPECTED_COPY` plus the client tag.
  - It asserts the tags of the event the route signed. It then asserts that a re-add is `unchanged` and sends nothing.
- **The withdraw row** (`app/routers/CLAUDE.md:133`) matches the code.
  - `withdrawn`, `unchanged` and `conflict` come from `_decide_and_sign`, and `failed` (out of time, or no relay took
    it) from `_send`.
  - The 502s come from `_read` and from `_press` (148–151).
  - A withdraw never answers `not_found`, and the row doesn't claim it.
- **ADR D1's correction** (lines 105–108) has the mechanism right.
  - At `cea36065`, `extractApiError` returns pydantic's `detail` list (`core.ts:265–268`).
  - `assistantJson` passes it to `new Error(…)` (`users.ts:160`).
  - The new sentence's "never" is too strong, though (Non-blocking 4).

### Collateral damage

Outside the target, one behaviour changed: a `d` can now carry a newline into a log line (Non-blocking 1). Nothing else
did:

- `git diff 6c0b9f7 e9adf7c -- app tests` changes three code lines and their comments;
- the only test line removed is B4's old comment;
- no dependency file changed;
- `parse_concept_coordinate`'s only callers are the schema and `_named`, and the reader's only caller is the service;
- the story's `Status` line is untouched.

**Gate A classification: unchanged.** The wider grammar is D1's own rule ("a `d` may contain colons and must be
non-empty"), now applied to the whole string, so it adds no trigger.

### Spec delta (the verdicts that move)

| Criterion | Before | Now |
| --- | --- | --- |
| AC8 input | Not met (Blocking 1) | **Met** (B4, R26; probes G, P6–P10, P12). One pre-existing edge: a lone surrogate answers 500, not 422 (Non-blocking 3) |
| AC1, AC11 | Met (on the composed tags; on the signed event by probe P2) | **Met**, now also pinned through signing by R27 |
| AC4 | Met | **Met.** A coordinate and its trailing-newline sibling are now two concepts, not a same-`d` conflict (P8) |

Every other criterion stands as the earlier table has it. The fix touches no code they rest on.

### Evidence table

| Claim | Evidence | Result |
| --- | --- | --- |
| Every accepted coordinate is exactly `39998:<pubkey>:<d>` | Probe G (60,000 values, 39 edges, up to 10 MB); P12 through `_named` | Holds |
| The fix changes only values containing `\n` | G's differential, pre-fix against fixed | 2,774 differ, all with `\n` |
| The schema and the parser agree | G over 60,000 values; P10 on both routes | Identical; strings unaltered; non-strings get 422 |
| The signed `b` names the copied header's coordinate | P6, P7, P9, P12 (300 fuzzed `d`s, real signing) | Holds (one exception: Non-blocking 2) |
| The new tests fail before the fix | The `6c0b9f7` export plus HEAD's three test files | 4 failed (B4 ×2, W9, R26); 98 passed, R27 among them |
| …and pass after it | The same files at HEAD | 102 passed |
| The reorder keeps behaviour for well-formed events | RD1–RD3 and W1–W9 on both trees | Same results |
| R27 is the UI fixture | R27's lists extracted from the source (AST), compared with B1's constants | Equal |
| The gate is green | The five stages above | All exit 0; 1,596 passed |
| Integration parity | HEAD and the `6c0b9f7` export; sorted ids; `diff` | Identical 143; refused connections |
| A `d` can forge a log line | Probe LG | HEAD: one forged line; `6c0b9f7`: 422 |
| A lone surrogate in `d` | P11 on both routes and both trees | 500, nothing sent |
| A first `d` tag with no value | Probe FD | Keyed `(P, "x")`; signed `b` = `39998:P:x`; nostr-sdk's `identifier()` is `None` |
| Documented line counts | `wc -l` against `app/services/CLAUDE.md:25–26` | 135 and 95, documented as 132 and 93 |

### Findings

#### Blocking

None.

#### Non-blocking

1. **`app/services/assistant_dictionary_service.py:309`: a caller-chosen `d` can now forge lines in the server's log.**
   This is new with the fix.
   - **Why it happens.** `_send` logs `f"dictionary copy {result.local} not on {line.url}: {line.message}"` for each
     relay that didn't take a copy. `result.local` ends in the `d`, which may now hold `\n`.
   - **Probe LG.**
     - A header was held at `d` = `"x\n2026-10-28 00:00:00,000 - app.core.auth - ERROR - FORGED LINE"`, with one
       outbox relay down.
     - The answer was 200 `published`, and the log gained the line `2026-10-28 00:00:00,000 - app.core.auth - ERROR -
       FORGED LINE not on wss://relay.primal.net: IO error: Connection refused`.
     - At `6c0b9f7` the same body was a 422.
   - **Who can do it.** Any signed-in customer. dcosl takes writes from any key (the earlier review's NIP-11 check).
     Any target that doesn't take the copy triggers the line, so an unreachable public relay is enough.
   - **Why it isn't blocking.**
     - It's plain-text log forging; nothing is signed wrongly.
     - `\r` and ANSI escapes already reached the same line before the fix.
     - Relay-supplied text is logged raw the same way elsewhere (`duty_menu_service.py:239`,
       `self_map_service.py:335`).
   - **Suggested.** `{result.local!r}`, a one-token change, best taken before merge.
2. **`app/services/dictionary_relay_reader.py:35–39` and `app/services/dictionary_copy_build.py:74`: a header whose
   first `d` tag has no value is read as the header at its next `d`.** This predates the fix.
   - **Two rules.**
     - The server takes "the first `d` with a value".
     - nostr-sdk (`Event.tags().identifier()` gives `None`) and the UI (`tagValue`, `dlistFields.ts:85–88` at
       `cea36065`) take the first `d`, and none when it has no value.
   - **Probe FD.**
     - P's header `[["d"], ["d", "x"], …]` verifies, and the reader keys it `(P, "x")`.
     - The Assistant then signs `["d", "x"]` with `b` = `39998:P:x`.
     - By nostr-sdk's rule and the UI's, that header has no coordinate at all.
   - **Why it isn't blocking.** Only the header's own author can make one. The copy carries that author's own
     definition, and withdrawing `39998:P:x` still removes it.
   - **Suggested.** Take the first `d` tag's value in both places, and skip a header whose first `d` has none.
3. **`app/services/dictionary_copy_build.py:58,66`: a `d` holding a lone surrogate passes the schema, and the route
   answers 500.** This predates the fix.
   - **Reach.** It can only arrive as a JSON `\ud800` escape.
   - **What happens** (with the story's fakes; it's the same at `6c0b9f7`).
     - Nothing is signed or sent: the service answers `not_found` (add) or `unchanged` (withdraw).
     - Starlette then can't encode the echoed `community` as UTF-8 (`starlette/responses.py:198`,
       `UnicodeEncodeError`).
   - **Why it matters at all.** No event can carry such a `d`, and AC8 asks for a 422.
   - **Suggested.** Have `parse_concept_coordinate` refuse a value that doesn't encode as UTF-8.
4. **Four claims in the docs are broader than the code.**
   - **"A malformed event is skipped"** (story line 230, test plan line 163, the commit message).
     - It's true of a malformed `created_at`.
     - A `tags` that isn't a list still raises at `dictionary_relay_reader.py:36`.
     - A `d` value that's a list or an object still raises at `:79` (RD4: `TypeError`, on both trees).
     - Both fail closed, as a 502, and strfry validates events on ingest. Either narrow the sentence or guard `_d_of`.
   - **"The UI never sends a bad body"** (ADR line 107).
     - `definitionOf` (`conceptResolution.ts:134` at `cea36065`) sends `coordinateOf(header) ?? header.id`.
     - So a community header with an empty or missing `d` sends `39998:<pk>:` or an event id. That's a 422, shown as
       "[object Object]".
     - It's rare; "only for a header without a `d`" would be accurate.
   - **Line counts.** `app/services/CLAUDE.md:25–26` still say 132 and 93. The fix made the files 135 and 95 lines.
   - **The story's gate table** (line 216) still records the pre-fix 1,591. The post-fix run appears only in the commit
     message.

**The earlier non-blocking items:**

- **Fixed:** 3 (`_newer`), this side of 5 (D1's sentence) and 6 (the withdraw row).
- **Fixed as a test:** 2, by R27. `_check` still compares only `d` and `b`.
- **Follow-ups, as the story says:** 1, 4, 7 and 8, and the UI side of 5.

### Harness friction

1. **The pre-fix recipe.** What worked was the whole-tree archive at the pre-fix commit, with the fix's test files
   copied over. That way the copied tests keep `tests/conftest.py`'s settings bootstrap and `pyproject.toml`'s pytest
   settings. It's worth recording beside friction 2's parity recipe.
2. **A fix round has no rule for what to refresh in the story.** The light profile could ask it to re-record the gate
   results and any line counts the fix moves.

### Verdict

**PASS** (mergeable as-is)

Blocking 1 is resolved:

- every coordinate the schema and the service accept is exactly `39998:<pubkey>:<d>`, and the two agree;
- for every `d` I tried, the signed `b` names the coordinate of the header copied;
- the new tests fail before the fix for the defect's own reasons, and pass after it.

Everything else holds:

- the gate is green under the approved deviation (1,596 passed), with integration parity id for id;
- the non-blocking fixes are correct;
- no other behaviour changed, apart from the newline that can now reach one log line (Non-blocking 1).

The four new findings are non-blocking, and nothing holds the gate. I'd take Non-blocking 1 (one token) before merge.
As before, the operator gives Gate B's verdict. This section is uncommitted, and the story's `Status` line is
untouched.

## Operator verdict (2026-10-31)

**PASS**, given in the session as "PASS, rebase, open PR". By then the re-review's four non-blocking findings were
fixed (`59aec9f`; the story's "Changes from the Gate B re-review"). The branch was then rebased onto `main` (the
story's "The rebase onto main").
