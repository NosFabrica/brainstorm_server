# Build Audit: Shortest-path (follow-graph) API endpoint

**Book:** `engineering-team/audits/shortest-path/book.md`
**Date:** 2026-07-13
**Branch / commit range:** `main..shortest-path` (7 commits, base 8dbc755)
**Provenance:** Acceptance-frame
**Confidence:** high

## 1. What shipped

- `GET /shortestPath?from=&to=&maxHops=&maxPaths=` — shortest directed
  `FOLLOWS` path(s) between two pubkeys: reachability, hop distance, one
  randomly selected representative path, exact path count with cap flag.
  Public read, hex or npub inputs in any mix —
  `stories/shortest-path/1-get-shortest-path.md` (Done, review PASS).

## 2. Epics & stories rolled up

### Epic: `shortest-path` (Status: Active — retirement to `done/` awaits merge)
| Story | Delivered | Status | Review |
|---|---|---|---|
| #1 get-shortest-path | The single-pair endpoint, guardrails, 19 tests | Done | `reviews/shortest-path/1-get-shortest-path.md` (PASS) |

## 3. As-built inventory

Derived from the diff (8 files, +614/−1):

- **User-facing:** one new route, `GET /shortestPath`, root-mounted (OpenAPI
  tag `graph`, visible at `/docs`). No auth. Response = standard
  `{code, message, data}` envelope; `data` keys camelCase (`from`, `to`,
  `reachable`, `hops`, `path`, `pathCount`, `pathCountCapped`, `maxHops`).
  Errors: 400 (unparseable pubkey, FastAPI `{"detail": str}` shape), 422
  (bounds/missing params, framework shape).
- **Domain:** reads Neo4j `NostrUser`/`FOLLOWS` only. No schema, migration,
  config/env, or dependency changes. No writes.
- **Code surface:** `app/repos/user_repo.py::get_all_shortest_follow_paths`
  (one `allShortestPaths` query: capped `collect` + true `count(*)`;
  `max_hops` interpolated behind an at-site guard, all values `$`-params);
  `app/services/graph_service.py` (npub→hex resolution, self-path
  short-circuit, one session per request, `random.choice` selection);
  `app/routers/graph/router.py` (+ registration in `app/routers/router.py`);
  `ShortestPathData` / `GetShortestPathResponse` in
  `app/schemas/request_response_schemas.py`.
- **Tests:** 12 fast (`tests/test_shortest_path.py`) + 7 integration with a
  seeded 6-node fixture (`tests/integration/test_shortest_path_integration.py`).

## 4. Deviations from intent

| # | Specified (anchor) | Built | Type | Rationale (source) | Product impact | Carry-forward |
|---|---|---|---|---|---|---|
| 1 | Issue #43 also specified `POST /shortestPath/batch` | Not built | deferred | Kickoff amendment (David, 2026-07-13) — frame narrowed before work began | List views (search results, connections) cannot batch-fetch degree badges yet | Batch endpoint story (design pre-agreed in issue #43) |
| 2 | Frame: 400 "with an error payload" (shape unspecified) | FastAPI `{"detail": "<msg>"}` | interpretation + constraint-discovered | Matches every live hand-raised error in the API; `ErrorResponseSchema` exists but is wired only into OpenAPI docs, never responses (review non-blocking #3; post-review product question, David 2026-07-13) | Error responses are not envelope-symmetric with success responses — API-wide condition, not endpoint-specific | Unify error responses (`ErrorResponseSchema` via exception handlers) as a cross-cutting story; GitHub issue planned |
| 3 | Issue #43 guardrails: "consider a query timeout" | No explicit Cypher timeout | intentional-change | ADR 0001 Decision 2 — no repo precedent; `maxHops ≤ 50` is the primary bound; mechanism named (`neo4j.Query(timeout=…)`) | Pathological queries rely on the hop bound + driver defaults | Timeout knob if latency data warrants |
| 4 | — | Integration-test transport reworked mid-phase (one event loop per test) | constraint-discovered | Conftest `TestClient` opens a loop per request; app-level Neo4j driver pool is loop-bound (test-plan Amendment, own commit) | None (test-only; assertions unchanged) | — |

**Undocumented work:** none — every hunk in `git diff main..HEAD -- app tests`
traces to story #1 / ADR 0001. (A machine-local `.env` was created for
integration testing; it is gitignored and not part of the branch.)

## 5. Quality state at close

- Test gate at close: fast suite `176 passed, 14 deselected`; story
  integration suite `7 passed` (live Neo4j). Same results as the review run.
- Known open issues on this endpoint: none.
- Debt observed (pre-existing, documented in the review, NOT introduced here):
  repo-wide `black --check` / `mypy` fail at `main` (36 reformattable files,
  ~80 mypy errors — `poe check_all` is aspirational); `user_repo.py` types
  Neo4j sessions as `AsyncNeoDriver` (every function, new one included,
  triggers the same mypy class); flake8 E231 fires inside f-strings on
  py3.12 (PEP 701), hitting Cypher f-strings old and new.

## 6. Carry-forward register

- [ ] `POST /shortestPath/batch` (deviation #1 — deferred at kickoff; spec already in issue #43).
- [ ] Unify API error responses — `ErrorResponseSchema` documented as the
      convention (`app/routers/CLAUDE.md`, `app/services/CLAUDE.md`) but never
      used in live responses; three error shapes exist today (deviation #2).
      GitHub issue to be filed.
- [ ] Per-query Cypher timeout knob (deviation #3, ADR 0001 Decision 2).
- [ ] flake8 policy for PEP-701 E231 inside Cypher f-strings (review
      non-blocking #1).
- [ ] Issue #43 "future work" retained upstream, untouched by this book:
      `via=` edge types, undirected variant, precomputed `hops_<observer>`
      fast path, path hydration.
