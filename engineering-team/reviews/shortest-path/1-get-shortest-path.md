# Review: Story 1 — GET /shortestPath (degree of separation between two pubkeys)

**Reviewer:** Claude (acting as Reviewer)
**Date:** 2026-07-13
**Diff:** `git diff main...HEAD -- app/ tests/` (HEAD = `bc4f4bc` "impl: get-shortest-path (story #1, ADR 0001)")
**Story:** `engineering-team/stories/shortest-path/1-get-shortest-path.md`
**ADR:** `engineering-team/decisions/shortest-path/0001-shortest-path-query-and-placement.md`
**Test plan:** `engineering-team/stories/shortest-path/1-get-shortest-path.test-plan.md` (incl. Amendment)

Branch commits audited: `af29d0d` intake → `14d1a5e` story → `04a8278` adr → `4f3c097` failing tests → `ad2b85f` test harness fix → `bc4f4bc` impl. The impl commit touches **only** `app/` (verified via `git show --stat bc4f4bc`), so all test content since the red commit comes from the amendment commit alone.

## Quality gates (run by reviewer, not trusted)

All run inside the `brainstorm-server-service` image with the repo mounted and the warm `brainstorm-test-venv` dependency volume, per the test plan's "How to run".

- [x] **Fast suite** — `poetry run pytest -m "not integration" -q` → **`176 passed, 14 deselected in 0.72s`**. Exactly the expected count (164 pre-existing + 12 new fast tests).
- [x] **Story integration suite** — `poetry run pytest tests/integration/test_shortest_path_integration.py -q` against the live local stack (`NEO4J_DB_URL=bolt://host.docker.internal:7688`) → **`7 passed in 0.19s`**.
- [x] **black --check** (scoped to touched files) — new files clean (`graph_service.py`, `graph/__init__.py`, `graph/router.py`, `request_response_schemas.py`, both test files). `app/routers/router.py` and `app/repos/user_repo.py` "would reformat" — **verified pre-existing**: the same blobs at `main` (piped via `git show main:<f>` into the identical container invocation) fail identically, and `black --diff` at HEAD shows hunks only at router.py:128 and user_repo.py:649/781 — none intersects the new lines (router include block 42–48 / import line 10; user_repo new function 826–872).
- [x] **isort --check** (scoped) — new files clean. `router.py`, `user_repo.py`, `request_response_schemas.py` fail — **verified pre-existing** (same failures on the `main` blobs). `isort --diff` at HEAD confirms none of the complaints touch the new/changed lines: the new `graph_router` import is already in sorted position, and the schemas complaint is the pre-existing `graperank_schemas`-after-`schemas` block ordering.
- [x] **flake8 --max-line-length 100** (scoped) — new files zero findings. `router.py`: 2×E231, identical at main (line 156 → 164, shifted by the +8 inserted lines; the known digest-f-string artifact). `user_repo.py`: main = 35 findings, HEAD = 37; the two new ones are `857:13` / `857:53` E231 **inside the new Cypher f-string** (`(a:NostrUser {{pubkey: ...` label colons) — the same PEP-701/py3.12 tokenization artifact class as pre-existing `813:17/49` and every other Cypher-in-f-string in the file. Same class, same repo convention; called out as known pre-existing behavior, not a new violation class. Noted as Non-blocking #1.
- [x] **mypy (no new error class)** — scoped run over `graph_service.py`, `graph/router.py`, `user_repo.py`, `request_response_schemas.py` → 19 errors, all in three classes, each demonstrated pre-existing on untouched code in the same run or on untouched files:
  - `attr-defined` `"AsyncDriver" has no attribute "run"` — new code: user_repo.py:862; pre-existing: 16 occurrences at lines 45–819 (all below the new function, i.e. main's code verbatim).
  - `arg-type` `AsyncSession … expected AsyncDriver` — new code: graph_service.py:54; pre-existing: user_service.py:119/186/250/299/314 (same session-typed-as-driver convention).
  - `import-untyped` (nostr_sdk stubs) — new code: graph_service.py:10; pre-existing: search/router.py:5, user_service.py:44.
  No new error class introduced.

## Diff walk (file by file)

- **`app/repos/user_repo.py`** (+46, purely appended, lines 826–872) — `get_all_shortest_follow_paths(session, from_pubkey, to_pubkey, max_hops, max_paths) -> tuple[list[list[str]], int]`. Signature matches the ADR verbatim. Guard **at the interpolation site** (line 853): `type(max_hops) is not int or not 1 <= max_hops <= 50` → `ValueError` (also rejects `bool`, since `type(True) is bool`). Only the validated int is f-string-interpolated (`*..{max_hops}`, line 858); both pubkeys and `max_paths` are `$`-params (lines 862–867, with defensive `int(max_paths)`). Single round trip returns capped chains + true count (`collect(chain)[..$max_paths]`, `count(*)`, line 860) = ADR query-shape Option A. `record is None` → `([], 0)` defensive fallback; the aggregate-only RETURN yields `([], 0)` for unknown pubkeys, which the passing AC3 integration tests confirm live. Docstring documents the same-node prohibition and points at the service short-circuit.
- **`app/routers/graph/__init__.py`** — empty package marker, standard.
- **`app/routers/graph/router.py`** (new, 47 lines) — thin router: `@router.get(path="/shortestPath", ...)`; params `from_` (alias `"from"`), `to`, `maxHops` `Query(default=30, ge=1, le=50)`, `maxPaths` `Query(default=1000, ge=1, le=1000)` (lines 24–45) — FastAPI bounds machinery supplies AC6's 422s. Body is exactly one service call wrapped in `GetShortestPathResponse` (lines 46–47). **No auth dependency — deliberate public read per story** ("Out of scope: Auth: none required"), documented in the module docstring. No repo imports.
- **`app/routers/router.py`** (+8) — sorted import (line 10) + root-mount include with `tags=["graph"]`, no prefix (lines 43–46), matching the open-ranking root-mount precedent the ADR cites. Repo-wide grep confirms no other route or path string claims `/shortestPath`.
- **`app/schemas/request_response_schemas.py`** (+25/−1) — `ShortestPathData` (lines 195–211) with `populate_by_name=True` and `serialization_alias` for `from`/`to`/`pathCount`/`pathCountCapped`/`maxHops`; `GetShortestPathResponse(SuccessfulResponseDataSchema)` (lines 214–215). Import line gains `ConfigDict, Field` (sorted). Matches the ADR field-for-field.
- **`app/services/graph_service.py`** (new, 81 lines) — `_resolve_pubkey_or_400` (lines 17–25, `PublicKey.parse(...).to_hex()`, mirrors the search router's resolution semantics, plain-string detail per ADR); resolution (34–35) → self-path short-circuit (40–50) → session open (52) → repo call → mapping. Unreachable branch returns `reachable=False, hops=None, path=None, path_count=0, path_count_capped=False` (57–66); reachable branch computes `hops=len(paths[0])-1` (safe: `paths` non-empty and all chains equal length by shortest-path minimality), `random.choice(paths)`, `min(path_count, max_paths)`, `path_count > max_paths` (68–81) — exactly the ADR mapping.
- **`tests/test_shortest_path.py`** (new, 12 tests) — AC4 self-path (hex; mixed hex/npub with hex-echo assertion) + AC6 validation (4×400 `from`, 1×400 `to`, 4×422 bounds, 1×422 missing param). Runs with zero mocking because both paths resolve before any Neo4j session — consistent with the code order verified below.
- **`tests/integration/test_shortest_path_integration.py`** (new, 7 tests) — module-scoped seeded 6-node/7-edge fixture with generated keys + `DETACH DELETE` teardown; never-seeded `ghost` key; covers AC1/AC2/AC3(×3)/AC5/AC6-equivalence. Harness: per-test `asyncio.run` over `httpx.ASGITransport` with a loop-local driver injected into `graph_service` and restored in `finally` (lines 103–127) — the Amendment's fix.

## Spec adherence — AC → test check

Every test below was executed by me in the gate runs above; all pass.

| Criterion | Test(s) | Level | Ran & passed |
|---|---|---|---|
| AC1 reachable pair, full shape, hex echo, default `maxHops` echo | `test_reachable_pair_full_shape` (also `code==200` wrapper check) | integration | Yes |
| AC2 random representative path (membership every call; ≥2 distinct chains over 40 calls, false-fire odds 2⁻³⁹) | `test_returned_path_is_random_member_of_shortest_set` | integration | Yes |
| AC3 unreachable — reverse-only edge | `test_reverse_only_edge_is_unreachable` | integration | Yes |
| AC3 unreachable — pubkey absent from graph | `test_unknown_pubkey_is_unreachable` | integration | Yes |
| AC3/AC1 `maxHops` gates reachability (2 → unreachable, 3 → hops 3; boundary distance == maxHops) | `test_maxhops_gates_reachability` | integration | Yes |
| AC4 self-path (hex) | `test_self_path_returns_zero_hops` | fast | Yes |
| AC4+AC6 self-path mixed hex/npub, hex echo | `test_self_path_accepts_mixed_hex_and_npub` | fast | Yes |
| AC5 `pathCount` cap + `pathCountCapped` (with `maxPaths=1`, the minimum bound) | `test_pathcount_is_capped_at_maxpaths` | integration | Yes |
| AC6 npub ≡ hex on real data, hex echo | `test_npub_and_hex_inputs_are_equivalent` | integration | Yes |
| AC6 malformed pubkeys → 400 (garbage, `nprofile1…`, 62-char hex, bad npub; `to` variant) | `test_invalid_from_pubkey_is_400[4 cases]`, `test_invalid_to_pubkey_is_400` | fast | Yes |
| AC6 bounds → 422 (`maxHops` 0/51, `maxPaths` 0/1001) | `test_out_of_bounds_params_are_422[4 cases]` | fast | Yes |
| AC6 missing required param → 422 | `test_missing_required_param_is_422` | fast | Yes |

- No criterion silently dropped. Defaults (30/1000) asserted via the `maxHops: 30` echo in AC1/AC4 tests and the `le=1000` bound tests.
- No behavior beyond the story: the diff adds one GET route, one service, one repo function, two schema classes. No batch endpoint, no `via=`, no hydration, no rate limiting, no auth changes.

## ADR adherence

- [x] Files changed = exactly the ADR's implementation-notes list (repo fn, service, schemas, graph router, router registration, two test files). Nothing else under `app/`.
- [x] Query shape = Option A (`collect(chain)[..$max_paths]` + `count(*)` in one round trip), user_repo.py:856–860.
- [x] Placement = Option A layering: router → service → repo; service owns resolution, short-circuit, session, random selection; repo owns only Cypher.
- [x] Decision 1 — self-path short-circuit **in the service, after npub→hex resolution, before any Cypher**: verified by code order, graph_service.py:34–35 (resolve) → 40–50 (short-circuit return) → 52 (first session open). Ratified by the operator per the ADR status line.
- [x] Decision 2 — no Cypher timeout in v1: no `timeout` anywhere in the new code; consistent with the ADR's deferred-knob decision.
- [x] No new dependencies: no `pyproject.toml`/`poetry.lock` change in the diff; all imports (`fastapi`, `nostr_sdk`, `neo4j`, `httpx`, `pydantic`, stdlib `random`/`asyncio`) already in the project.

## Repo-conventions integrity

*(Replaces the template's "Concept-graph integrity" — this repo has no concept graph.)*

- [x] **All Cypher lives in `app/repos/user_repo.py`** — the only Cypher added is in `get_all_shortest_follow_paths` (user_repo.py:826–872); the service and router contain none.
- [x] **Values `$`-parametrized; only the validated int bound interpolated, guard at the interpolation site** — `$from_pubkey`, `$to_pubkey`, `$max_paths` are query parameters (user_repo.py:856–867); `{max_hops}` is the sole interpolation (858) and the type+range guard immediately precedes it in the same function (853–854), independent of the router's `ge`/`le`. Matches the existing structural-token precedent (`_get_pubkeys_with_influence`, user_repo.py:21–50).
- [x] **Layering** — router imports only the service and the schema (graph/router.py:13–14); the service opens the session via `async with neo4j_driver.session() as session:` (graph_service.py:52) and passes it to the repo. No router→repo import.
- [x] **Everything async** — endpoint, service, repo are `async def`; the only sync work is `PublicKey.parse` and `random.choice` (trivial CPU, no blocking I/O).
- [x] **Response wrapper + camelCase** — `GetShortestPathResponse(SuccessfulResponseDataSchema)` (request_response_schemas.py:214); public keys via `serialization_alias` (`from`/`to`/`pathCount`/`pathCountCapped`/`maxHops`, lines 204–211; `from` is a Python keyword, hence the alias). FastAPI's by-alias serialization confirmed by the passing tests asserting `data["from"]`, `data["pathCount"]`, etc.
- [x] **Public read, but inputs validated before any query** — pubkeys resolved-or-400 before the session opens (graph_service.py:34–35 vs 52); numeric bounds 422 at the framework boundary (graph/router.py:33–45); repo guard as defense in depth. No auth dependency on the route — deliberate per story, not a finding.

## Things tests can't catch (each demonstrated)

- [x] **No-test-weakening audit** — `git diff 4f3c097..HEAD -- tests/` touches only `tests/integration/test_shortest_path_integration.py` (`tests/test_shortest_path.py` and `tests/conftest.py`: zero delta). I extracted the file at `4f3c097` and compared all 7 tests one by one against HEAD: identical requests (same params incl. `maxHops`/`maxPaths` overrides) and identical assertions in all of `test_reachable_pair_full_shape`, `test_npub_and_hex_inputs_are_equivalent`, `test_returned_path_is_random_member_of_shortest_set` (same `N_RANDOM_CALLS = 40`, same `len(seen) == 2`), `test_reverse_only_edge_is_unreachable`, `test_unknown_pubkey_is_unreachable`, `test_maxhops_gates_reachability`, `test_pathcount_is_capped_at_maxpaths`. Changes are transport-only, exactly as the Amendment describes: `_api()` context manager (lines 103–127) swapping the conftest `TestClient` for `httpx.ASGITransport` + per-test `asyncio.run` + loop-local driver injected into `graph_service` and restored in `finally`; `_fresh_driver()` de-dup helper; `PublicKey` import moved to module top. Nothing weakened, nothing dropped.
- [x] **Random selection over the capped set; comment matches behavior** — graph_service.py:74–77: `random.choice(paths)` where `paths` is the repo's already-capped list (Cypher slices before returning, user_repo.py:860); the adjacent comment says exactly that ("pick from the capped sample (documented sampling bias, issue #43)"). Comment and behavior agree.
- [x] **Self-path ordering** — resolution (34–35) → short-circuit (40–50) → session (52); see ADR adherence above.
- [x] **Hex echo (AC1/AC6)** — the service constructs every response from `from_hex`/`to_hex` (graph_service.py:42–43, 58–59, 69–70), never the raw inputs; asserted by `test_self_path_accepts_mixed_hex_and_npub` (fast) and `test_npub_and_hex_inputs_are_equivalent` (integration), both passing.
- [x] **Edge semantics** — unknown pubkey and reverse-only edge return 200-unreachable via the aggregate `([], 0)` path (user_repo.py:868–872 → graph_service.py:57–66), proven live by the two AC3 integration tests; `maxHops` honored end-to-end (`ge=1, le=50` at graph/router.py:33–38 + repo guard 853 + `test_maxhops_gates_reachability`).
- [x] **No secrets** — the diff contains no credentials; all test keys are `Keys.generate()`d per run; integration config comes from env/`.env`, not the committed files.
- [x] **No leftover debug logging / prints** — grep over all four new files for `print(`, `logger`, `loggr`, `logging.`, `breakpoint`, `pdb`: zero hits. The new code does no logging at all (nothing to route through `app.core.loggr`; no stray stdlib logging introduced).
- [x] **No commented-out code, no stray TODO/FIXME** — same grep sweep (TODO/FIXME/XXX): zero hits; full-diff read confirms comments are explanatory only.
- [x] **No scope creep** — `app/` delta is exactly the story's endpoint (no batch route, no extra endpoints, no config/env changes, no migrations). The `engineering-team/` additions are the process artifacts of this story/epic. (`docs/proposals/` sits untracked in the working tree — not part of any branch commit, not reviewed here.)
- [x] **Concurrency** — stateless endpoint; one session per request via context manager (the repo's "session is the unit of work" rule); shared module-level driver pool is the established pattern. The integration harness's module-global driver swap is test-only and restored in `finally`.
- [x] **Error paths** — unparseable pubkey → 400 with `{"detail": ...}` payload, no stack trace (fast tests); bounds/missing → 422 (framework); Neo4j-down → 500 is explicitly accepted by the test plan ("no degraded-mode requirement in the story").
- [x] **Injection surface** — no f-string value interpolation besides the guarded int; pubkeys reach Cypher only after `PublicKey.parse` round-trip normalization (64-char lowercase hex), and even then only as `$`-params.

## House rules check

- [x] No new lint/typecheck/build tooling introduced (no config, no hooks, no dependency changes).
- [x] Cypher placement / parametrization authority respected (see Repo-conventions integrity).

## Findings

### Blocking

None.

### Non-blocking

1. **app/repos/user_repo.py:857** — the new Cypher f-string adds 2 flake8 E231 hits (Cypher label colons tokenized by flake8 under py3.12/PEP 701). Same artifact class as pre-existing line 813 and unavoidable while following the file's Cypher-in-f-string convention. Optional improvement (out of this story's scope): a repo-wide flake8 `extend-ignore`/`noqa` policy for Cypher f-strings so touched-file lint runs can go fully green.
2. **app/services/graph_service.py:21** — `except Exception` around `PublicKey.parse` is broad, but mirrors the search router's `_try_resolve_pubkey` precedent and wraps a pure parse call; acceptable as-is.
3. **app/services/graph_service.py:22–25** — plain-string `HTTPException` detail rather than the `ErrorResponseSchema` detail some services use. This is the ADR's explicit choice (search-router precedent; story open question 3 resolved on approval), so it is conformant — recorded here only so a future reviewer doesn't re-litigate it.
4. **ADR Decision 2 reminder** — no Cypher timeout: a pathologically dense pair holds its session until the driver default applies. Accepted for v1 with the knob named (`neo4j.Query(text, timeout=…)`); revisit with production data.

## Verdict

**PASS**

All six acceptance criteria are covered by tests I ran myself (176 fast / 7 integration, exact expected counts); the diff matches the ADR file-for-file and decision-for-decision; layering, parametrization, aliasing, and the self-path ordering are verified with line-level evidence; every style/type-gate deviation on touched files is demonstrated pre-existing (or the known py3.12 f-string artifact class) rather than introduced; and the post-red test changes are transport-only with assertions provably unchanged. Mergeable as-is.
