# ADR 0001: /shortestPath — single-query allShortestPaths behind a graph service

**Status:** Accepted (2026-07-13 — Decision 1's self-path short-circuit
explicitly ratified by David: "If from == to … skip the cypher query. Just
return a result of 0.")
**Date:** 2026-07-13
**Story:** `engineering-team/stories/shortest-path/1-get-shortest-path.md`

## Context

The story (shortest-path #1) requires `GET /shortestPath?from=&to=` over the
directed Neo4j `FOLLOWS` graph returning: reachable / hops / one randomly
selected shortest path / pathCount (+capped flag), honoring `maxHops` (1–50,
default 30) and `maxPaths` (1–1000, default 1000); inputs hex **or** npub in
any mix; 400 on unparseable pubkeys, 422 on bounds; public read; AC1–AC6 in
the story file.

Relevant facts, verified against the codebase and the **live local stack**
(2026-07-13):

- **Repo conventions** (`app/repos/CLAUDE.md`): all Cypher lives in
  `app/repos/user_repo.py`; functions are `async def fn(session:
  AsyncNeoDriver, ...)`; **values** are always `$`-parametrized; only
  structural tokens from fixed sets are f-string-interpolated (existing
  precedent: `relation`/`direction` in `_get_pubkeys_with_influence`,
  `user_repo.py:21-50`). Layering (`app/services/CLAUDE.md`,
  `app/routers/CLAUDE.md`): routers thin-wrap services; **services** open the
  Neo4j session (`async with neo4j_driver.session() as session:`, import per
  `user_service.py:17`) and raise `HTTPException` themselves.
- **Neo4j rejects same-node shortest paths.** Verified live:
  `allShortestPaths((a)-[:FOLLOWS*..30]->(a))` errors with "The shortest path
  algorithm does not work when the start and end nodes are the same." The
  story's AC4 (`from == to` → hops 0) therefore **cannot** be answered by
  Cypher; it must short-circuit in the app layer.
- **The issue's query shape works.** Verified live on a real 2-hop pair:
  `MATCH p = allShortestPaths((a)-[:FOLLOWS*..30]->(b)) WITH [n IN nodes(p) |
  n.pubkey] AS chain RETURN collect(chain)[..$maxPaths], count(*)` returns the
  capped chains **and** the true shortest-path count in one round trip.
  Variable-length bounds cannot be `$`-parametrized in Cypher (`*..$maxHops`
  is illegal) — the bound must be interpolated as a validated integer.
- **`nostr_sdk.PublicKey.parse`** accepts 64-char hex (either case) and
  `npub1…`, normalizes via `.to_hex()` (lowercase), and raises on anything
  else (verified: garbage, truncated hex, `nprofile1…` all rejected). Existing
  precedent: `_try_resolve_pubkey` in `app/routers/search/router.py:30`.
- **Response wrapper convention**: success responses subclass
  `SuccessfulResponseDataSchema` in `app/schemas/request_response_schemas.py`
  (e.g. `SearchByTextResponse`, line 191). `from` is a Python keyword, so the
  schema and the query param need aliasing.
- **Root-mount precedent**: `open_ranking_router` is included with no prefix
  because its protocol mandates exact paths (`app/routers/router.py:36-40`);
  `/shortestPath` needs the same.
- No prior ADRs exist in this repo (this is ADR 0001; no conflicts to check).

## Options considered

### Query shape

**Option A — single `allShortestPaths` query: `collect(chain)[..$maxPaths]` + `count(*)`** *(issue-prescribed)*
One round trip returns both the capped materialized chains and the **true**
count, so `pathCount = min(true, maxPaths)` and `pathCountCapped = true >
maxPaths` are exact.
*Pro:* one query; exact cap semantics; matches the issue's spec verbatim;
verified live.
*Con:* Cypher evaluates the full `collect(...)` before slicing, so memory is
proportional to (total shortest paths × path length) before the cap applies.
Bounded in practice by `maxHops` and by shortest-path minimality, but a
pathological dense pair could spike.

**Option B — `WITH p LIMIT $maxPaths` before collecting**
Bounds materialization server-side.
*Pro:* memory strictly bounded by `maxPaths`.
*Con:* loses the true count — `pathCount` becomes `min(true, maxPaths)` with
no way to distinguish "exactly maxPaths" from "more"; `pathCountCapped`
degrades to `pathCount == maxPaths` ("may exceed"). Two queries would be
needed to recover the count.

**Option C — two queries: `shortestPath` for hops, then bounded enumeration**
*Pro:* cheapest possible when only hops matter (that's the deferred batch
variant's shape, not this story's).
*Con:* two round trips per request for no v1 benefit; more code.

### Code placement

**Option A — new root-mounted `graph` router + new `graph_service` + repo fn in `user_repo.py`**
Follows the repo's stated layering (routers → services → repos) for new code;
the service owns pubkey resolution, the self-path short-circuit, the session,
and random selection; the repo owns only Cypher.
*Pro:* each concern in its conventional home; the deferred batch endpoint
lands in the same router/service later.
*Con:* two new small modules.

**Option B — search-router style: logic in the router, no service**
Precedent exists (`search/router.py` resolves pubkeys and calls `vespa`
directly).
*Pro:* one file.
*Con:* contradicts the documented convention ("Routers do not import repos
directly. Services do." — `app/services/CLAUDE.md`); search predates that
guidance and talks to Vespa, not the repos layer.

## Decision

**Query shape: Option A** — the issue prescribes it, it's verified live, and
exact `pathCount`/`pathCountCapped` semantics are part of the approved story
(AC1/AC5). The memory tradeoff is accepted for v1: `maxHops ≤ 50` and
shortest-path minimality bound the realistic path set, and Option B remains a
documented fallback if profiling ever shows a problem.

**Placement: Option A** — follow the layering convention for new code.

Two point decisions recorded with rationale:

1. **`from == to` short-circuits in the service** (after npub→hex resolution,
   before any Cypher): return `reachable: true, hops: 0, path: [pk],
   pathCount: 1, pathCountCapped: false` with **no graph lookup**. Forced by
   the verified Neo4j same-node error; AC4 does not require an existence
   check, and adding one would cost a query for no story value.
2. **No explicit Cypher timeout in v1.** The issue says "consider a timeout";
   there is no timeout precedent in `user_repo.py`, and the primary guardrail
   is the bounded `maxHops` (≤ 50) + `maxPaths` caps. A per-query timeout
   (`neo4j.Query(text, timeout=…)`) is named here as the follow-up knob if
   the endpoint ever shows pathological latency. Deferred, not forgotten:
   listed in Consequences and the story's linked-review scope.

## Consequences

- The deferred batch variant later reuses the same router/service and adds
  only a cheap `shortestPath((a)-[:FOLLOWS*..k]->(b))` repo function
  (Option C's shape) — nothing in v1 blocks it.
- Worst-case memory of `collect(...)` before the slice is accepted and
  documented; the fallback (Option B) changes only the repo function.
- No timeout means a pathological query holds its one session until the
  driver default applies; acceptable at current scale, revisit with data.
- New modules `app/routers/graph/` and `app/services/graph_service.py` are
  one more layer than the older search router uses; that's deliberate
  convention-following, not scope creep.
- No new dependencies, no schema/infra change, no config/env change.

## Implementation notes

- **`app/repos/user_repo.py`** — add:
  ```python
  async def get_all_shortest_follow_paths(
      session: AsyncNeoDriver,
      from_pubkey: str,
      to_pubkey: str,
      max_hops: int,
      max_paths: int,
  ) -> tuple[list[list[str]], int]:
  ```
  Guard at the interpolation site (defense in depth, independent of router
  bounds): `max_hops` must be an `int` with `1 <= max_hops <= 50`, else
  `ValueError`. Cypher (only the validated int is interpolated; every value is
  a `$`-param):
  ```
  MATCH (a:NostrUser {pubkey: $from_pubkey}), (b:NostrUser {pubkey: $to_pubkey})
  MATCH p = allShortestPaths((a)-[:FOLLOWS*..{max_hops}]->(b))
  WITH [n IN nodes(p) | n.pubkey] AS chain
  RETURN collect(chain)[..$max_paths] AS paths, count(*) AS path_count
  ```
  Zero rows (unknown pubkey(s)) or empty `paths` → return `([], 0)`.
- **`app/services/graph_service.py`** (new) —
  `async def get_shortest_follow_path(from_raw, to_raw, max_hops, max_paths) -> ShortestPathData`:
  1. `_resolve_pubkey_or_400(value, param_name)`: `PublicKey.parse(value).to_hex()`
     in try/except → `HTTPException(status_code=400, detail=f"{param_name} is
     not a valid hex pubkey or npub")` (mirrors
     `search/router.py:_try_resolve_pubkey` semantics; plain-string detail per
     search-router precedent).
  2. Self-path short-circuit (post-resolution) per Decision 1.
  3. `async with neo4j_driver.session() as session:` → repo call (import
     `from app.neo4j_db.driver import driver as neo4j_driver`).
  4. Map results: empty → `reachable=False, hops=None, path=None, pathCount=0,
     pathCountCapped=False`; else `hops = len(paths[0]) - 1`,
     `path = random.choice(paths)` (stdlib), `pathCount = min(path_count,
     max_paths)`, `pathCountCapped = path_count > max_paths`.
  5. Echo `from`/`to` as resolved hex + the effective `maxHops`.
- **`app/schemas/request_response_schemas.py`** — add:
  `ShortestPathData(BaseModel)` with `model_config = ConfigDict(populate_by_name=True)`
  and fields `from_pubkey: str = Field(serialization_alias="from")`,
  `to_pubkey: str = Field(serialization_alias="to")`, `reachable: bool`,
  `hops: int | None`, `path: list[str] | None`, `path_count: int`
  (`serialization_alias="pathCount"`), `path_count_capped: bool`
  (`serialization_alias="pathCountCapped"`), `max_hops: int`
  (`serialization_alias="maxHops"`); and
  `GetShortestPathResponse(SuccessfulResponseDataSchema)` with
  `data: ShortestPathData`.
- **`app/routers/graph/router.py`** (new) — `APIRouter`;
  `@router.get(path="/shortestPath", summary="Shortest directed FOLLOWS path between two pubkeys")`;
  params: `from_: str = Query(..., alias="from")`, `to: str = Query(...)`,
  `maxHops: int = Query(default=30, ge=1, le=50)`,
  `maxPaths: int = Query(default=1000, ge=1, le=1000)`; no auth dependency
  (public read per story); body = one `await graph_service.get_shortest_follow_path(...)`
  wrapped in `GetShortestPathResponse(data=...)`. FastAPI's `ge`/`le`/required
  machinery supplies the 422s of AC6.
- **`app/routers/router.py`** — `from app.routers.graph.router import router
  as graph_router` + `router.include_router(router=graph_router,
  tags=["graph"])` with **no prefix** (root mount, open-ranking precedent) so
  the path is exactly `/shortestPath`.
- **Tests** (designed next phase; placement per suite conventions in
  `tests/conftest.py`): fast mocked suite at `tests/test_shortest_path.py`
  (TestClient, mocked Neo4j session); integration suite at
  `tests/integration/test_shortest_path_integration.py` with the
  `integration` marker (real local Neo4j, seeded ~5-node fixture).

## Out of scope

- The batch endpoint, `via=` edge-type generalization, undirected variant,
  precomputed-hops fast path (story Out of scope; the design above leaves all
  of them clean extension points).
- Query timeout knob (Decision 2 — deferred with the mechanism named).
- Caching / rate limiting.
- Any change to the search router's own pubkey resolution.
