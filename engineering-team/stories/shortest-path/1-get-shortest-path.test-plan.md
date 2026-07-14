# Test Plan: Story 1 — GET /shortestPath

**Story:** `engineering-team/stories/shortest-path/1-get-shortest-path.md`
**ADR:** `engineering-team/decisions/shortest-path/0001-shortest-path-query-and-placement.md`
**Date:** 2026-07-13

## Coverage map

| Criterion | Test name | Test file | Level |
|---|---|---|---|
| AC1 reachable pair (full shape, hex echo, defaults) | `test_reachable_pair_full_shape` | `tests/integration/test_shortest_path_integration.py` | integration |
| AC2 random representative path (membership every call + ≥2 distinct over 40 calls) | `test_returned_path_is_random_member_of_shortest_set` | `tests/integration/test_shortest_path_integration.py` | integration |
| AC3 unreachable — reverse-only edge | `test_reverse_only_edge_is_unreachable` | `tests/integration/test_shortest_path_integration.py` | integration |
| AC3 unreachable — pubkey not in graph | `test_unknown_pubkey_is_unreachable` | `tests/integration/test_shortest_path_integration.py` | integration |
| AC3/AC1 `maxHops` gates reachability (2 → unreachable, 3 → 3 hops) | `test_maxhops_gates_reachability` | `tests/integration/test_shortest_path_integration.py` | integration |
| AC4 self-path short-circuit (hex) | `test_self_path_returns_zero_hops` | `tests/test_shortest_path.py` | fast (no backend) |
| AC4 + AC6 self-path, mixed hex/npub + hex echo | `test_self_path_accepts_mixed_hex_and_npub` | `tests/test_shortest_path.py` | fast (no backend) |
| AC5 `pathCount` cap + capped flag | `test_pathcount_is_capped_at_maxpaths` | `tests/integration/test_shortest_path_integration.py` | integration |
| AC6 npub ≡ hex on real data + hex echo | `test_npub_and_hex_inputs_are_equivalent` | `tests/integration/test_shortest_path_integration.py` | integration |
| AC6 malformed `from` (garbage / nprofile / short-hex / bad npub) → 400 | `test_invalid_from_pubkey_is_400[…]` (4 cases) | `tests/test_shortest_path.py` | fast |
| AC6 malformed `to` → 400 | `test_invalid_to_pubkey_is_400` | `tests/test_shortest_path.py` | fast |
| AC6 bounds → 422 (`maxHops` 0/51, `maxPaths` 0/1001) | `test_out_of_bounds_params_are_422[…]` (4 cases) | `tests/test_shortest_path.py` | fast |
| AC6 missing required param → 422 | `test_missing_required_param_is_422` | `tests/test_shortest_path.py` | fast |

Level rationale: AC4 and AC6-validation resolve before any Neo4j session
(ADR 0001), so they run in the fast suite with zero mocking. Everything
touching real graph semantics runs in the integration suite against the live
local Neo4j — no mocked-session middle layer, per the "tests must fail because
the feature is missing, not because of scaffolding" rule.

## Edge cases

- [x] Reverse-only edge (directedness) — AC3 test.
- [x] Pubkey absent from the graph entirely — AC3 test.
- [x] Distance exactly == maxHops (boundary: `maxHops=3` reaches a 3-hop pair).
- [x] `maxPaths=1` (minimum bound) with multiple shortest paths — AC5 test.
- [x] Non-npub NIP-19 form (`nprofile1…`) rejected — 400 case.
- [x] Uppercase hex accepted (implicitly: `PublicKey.parse` verified case-insensitive at ADR time; canonical lowercase echo asserted in AC1/AC6 tests).
- Not covered (accepted): concurrent calls (stateless endpoint, no shared
  mutable state beyond the driver pool); Neo4j down → FastAPI 500 (no
  degraded-mode requirement in the story).

## Test infrastructure

- Framework: pytest (`[tool.pytest.ini_options]` in `pyproject.toml`);
  `integration` marker already registered there.
- Fast suite: conftest `client` fixture (TestClient over `app.api:app`, no
  lifespan, no real services needed).
- Integration suite: real Neo4j from `settings.neo4j_db_url`. On this machine
  that's `bolt://localhost:7688` via the repo `.env` (created 2026-07-13 —
  note: host port 7688, NOT 7687, which belongs to the unrelated `tapestry`
  container; `.env` intentionally omits `PERIODIC_GRAPERANK_PUBKEY` because
  the open-ranking e2e suite asserts the conftest dummy observer).
- Fixture: module-scoped seeded graph (6 synthetic `NostrUser` nodes, 7
  `FOLLOWS` edges, freshly generated keys each run — no collision with organic
  data; `DETACH DELETE` teardown). One extra generated pubkey (`ghost`) is
  never seeded.
- No new test frameworks or dependencies introduced.

## How to run

No Python 3.12/poetry on the host — run inside the server image with the
source mounted and dev deps in a persistent volume (one-time
`poetry install --no-root` warm-up already done):

```bash
# fast suite (no services needed)
docker run --rm -v "$PWD":/app -v brainstorm-test-venv:/opt/.cache \
  brainstorm-server-service poetry run pytest -m "not integration" -q

# story tests incl. integration (local stack must be up; host ports via host.docker.internal)
docker run --rm -v "$PWD":/app -v brainstorm-test-venv:/opt/.cache \
  -e NEO4J_DB_URL=bolt://host.docker.internal:7688 \
  -e REDIS_HOST=host.docker.internal \
  -e DB_URL=postgresql+asyncpg://postgres:postgrespw@host.docker.internal:5432/brainstorm-database \
  -e VESPA_URL=http://host.docker.internal:8081 \
  brainstorm-server-service poetry run pytest \
  tests/test_shortest_path.py tests/integration/test_shortest_path_integration.py -v
```

## Verification

The new tests fail with the current code, all for the right reason (the route
does not exist → 404 against expected 200/400/422; the integration fixture
seeds and tears down cleanly — no import or setup errors). Confirmed
2026-07-13 at commit `adr: shortest-path …`:

```
FAILED tests/test_shortest_path.py::test_self_path_returns_zero_hops - assert...
FAILED tests/test_shortest_path.py::test_self_path_accepts_mixed_hex_and_npub
FAILED tests/test_shortest_path.py::test_invalid_from_pubkey_is_400[not-a-pubkey]
FAILED tests/test_shortest_path.py::test_invalid_from_pubkey_is_400[nprofile1qqstest]
FAILED tests/test_shortest_path.py::test_invalid_from_pubkey_is_400[8f3fbb...c72a73c6fa9]
FAILED tests/test_shortest_path.py::test_invalid_from_pubkey_is_400[npub1invalidinvalidinvalid]
FAILED tests/test_shortest_path.py::test_invalid_to_pubkey_is_400 - assert 40...
FAILED tests/test_shortest_path.py::test_out_of_bounds_params_are_422[overrides0]
FAILED tests/test_shortest_path.py::test_out_of_bounds_params_are_422[overrides1]
FAILED tests/test_shortest_path.py::test_out_of_bounds_params_are_422[overrides2]
FAILED tests/test_shortest_path.py::test_out_of_bounds_params_are_422[overrides3]
FAILED tests/test_shortest_path.py::test_missing_required_param_is_422 - asse...
FAILED tests/integration/...::test_reachable_pair_full_shape
FAILED tests/integration/...::test_npub_and_hex_inputs_are_equivalent
FAILED tests/integration/...::test_returned_path_is_random_member_of_shortest_set
FAILED tests/integration/...::test_reverse_only_edge_is_unreachable
FAILED tests/integration/...::test_unknown_pubkey_is_unreachable
FAILED tests/integration/...::test_maxhops_gates_reachability
FAILED tests/integration/...::test_pathcount_is_capped_at_maxpaths
============================== 19 failed in 0.24s ==============================
```

Sample failure detail (right-reason check): `assert 404 == 200 — where 404 =
<Response [404 Not Found]>.status_code`.

Full fast suite with the new tests present: `12 failed, 164 passed,
14 deselected` — the 12 failures are exactly this story's fast tests; nothing
pre-existing broke.
