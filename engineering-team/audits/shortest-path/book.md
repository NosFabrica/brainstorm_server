# Book of Work: Shortest-path (follow-graph) API endpoint

**Slug:** shortest-path
**Status:** Open
**Opened:** 2026-07-13
**Closed:** —

## Intent anchor
**Acceptance frame (no PRD)** — the ask is GitHub issue #43
(https://github.com/NosFabrica/brainstorm_server/issues/43), restated below and
confirmed at kickoff. Completion is *judged* against these bullets.

### Acceptance frame

- [ ] `GET /shortestPath?from=<hex>&to=<hex>` computes the shortest directed
      path(s) through the Neo4j `FOLLOWS` graph and returns: `reachable`,
      `hops` (null when unreachable), one **randomly selected** shortest path
      as a pubkey chain inclusive of both endpoints, `pathCount`, and
      `pathCountCapped` — honoring `maxHops` (default 30, bounded) and
      `maxPaths` (default 1000, capped).
- [ ] The endpoint is **public read** (no required auth), matching the
      posture of the `/user/{pubkey}/*` lookups.
- [ ] Guardrails hold: `from`/`to` validated as pubkeys (400 on malformed);
      pubkeys parametrized in Cypher (never string-interpolated); `maxHops`
      validated as a bounded integer **before** interpolation into the
      variable-length pattern; `maxPaths` capped with the cap surfaced;
      one Neo4j session per request.
- [ ] Code placement follows repo conventions: Cypher in
      `app/repos/user_repo.py`, a small dedicated router mounted at root,
      Pydantic request/response schemas, and an integration test against real
      Neo4j with a seeded ~5-node `NostrUser`/`FOLLOWS` fixture.
- [ ] Out of scope for v1 (explicitly): **the `POST /shortestPath/batch`
      endpoint (deferred at kickoff, 2026-07-13)**, `via=` edge types beyond
      `FOLLOWS`, undirected/mutual-degree variant, precomputed
      `hops_<observer>` fast path, path node hydration (names/avatars).

**Kickoff amendments (2026-07-13, David):** batch endpoint dropped from v1
scope; auth posture resolved — public read. Frame confirmed as amended.

## Epics in this book
- `shortest-path` — the two endpoints + guardrails + tests from issue #43.

## Provenance
- **Mode:** Acceptance-frame
- **Confidence at close:** *(set at close)*

## Close artifacts *(filled at book close)*
- Build audit: `engineering-team/audits/shortest-path/audit.md`
- Product feedback: `engineering-team/audits/shortest-path/prd-seed.md`
