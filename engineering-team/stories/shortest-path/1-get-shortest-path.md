# Story 1: GET /shortestPath — degree of separation between two pubkeys

**Status:** Approved
**Created:** 2026-07-13
**Type:** Feature

## Background

The Brainstorm profile page wants a LinkedIn-style "1st / 2nd / 3rd degree"
indicator: how far is the viewer (Alice) from the profile owner (Bob) through
the directed follow graph? More generally, any client should be able to ask
"how is A connected to B, and through whom?" without that query being welded to
the `/user` resource domain.

`FOLLOWS` is a directed relationship, so the query is inherently two-argument:
the path Alice → Bob is not the path Bob → Alice. Named `from`/`to` query
parameters make the direction explicit and self-documenting.

Source spec: GitHub issue #43 (endpoint design, semantics, and guardrails were
agreed there). Scope was narrowed at kickoff: v1 is this single-pair endpoint
only.

## User-facing description

As a Brainstorm client (e.g. the profile page), I want to ask the server for
the shortest directed follow-path from one pubkey to another, so that I can
display the degree of separation and a representative "who connects them"
chain.

## Acceptance criteria

Testable from the outside. Each criterion gets at least one test.

- [ ] **AC1 — reachable pair.** Given a graph where the shortest directed
      `FOLLOWS` chain from A to B has k edges (k ≤ `maxHops`),
      `GET /shortestPath?from=A&to=B` returns HTTP 200 with: `reachable: true`,
      `hops: k`, `path` = a list of k+1 hex pubkeys starting at A and ending at
      B that traces a real shortest path, `pathCount` = the exact number of
      distinct shortest paths (when ≤ `maxPaths`), `pathCountCapped: false`,
      and the response echoes `from`, `to`, and the effective `maxHops`.
      `from`/`to` are echoed in **hex** (an npub input is echoed as its
      resolved hex), matching the hex pubkeys in `path`.
- [ ] **AC2 — random representative path.** The returned `path` is always a
      member of the set of shortest paths. When several shortest paths exist,
      repeated calls do not always return the same one (random selection; a
      statistical test over N calls is acceptable).
- [ ] **AC3 — unreachable.** When no directed path from A to B exists within
      `maxHops` — including: only the reverse direction exists (B→A),
      either pubkey has no node in the graph, or the true distance exceeds
      `maxHops` — the endpoint returns HTTP 200 with `reachable: false`,
      `hops: null`, `path: null`, `pathCount: 0`, `pathCountCapped: false`.
- [ ] **AC4 — self-path.** `from` == `to` (valid pubkey) returns HTTP 200 with
      `reachable: true`, `hops: 0`, `path: [pubkey]`, `pathCount: 1`,
      `pathCountCapped: false`. *(Proposed — the issue is silent on this
      edge; resolves on story approval.)*
- [ ] **AC5 — path-count cap.** When the number of shortest paths exceeds
      `maxPaths`, the response has `pathCount == maxPaths`,
      `pathCountCapped: true`, and `path` is drawn from the capped sample.
- [ ] **AC6 — input formats & validation.** `from` and `to` each accept a
      64-char hex pubkey **or** an `npub1…` (NIP-19) encoding, in any
      combination (e.g. `from` hex, `to` npub); an npub input behaves
      identically to its hex equivalent (resolved to hex before the graph
      query). An input that is neither valid hex nor a decodable npub →
      HTTP 400 with an error payload (no stack trace, no 5xx). `maxHops`
      outside 1–50, `maxPaths` outside 1–1000, or a missing required
      parameter → HTTP 422 (framework-native bounds validation). No input
      may produce a 5xx.

Defaults: `maxHops` = 30, `maxPaths` = 1000.

## Concepts touched

*(No concept-graph in this repo — plain references.)*

- `NostrUser` — Neo4j node label, keyed by hex `pubkey` property.
- `FOLLOWS` — directed Neo4j relationship between `NostrUser` nodes; the only
  edge type traversed in v1.
- Hex pubkey — 64-char hex string; canonical form in responses and graph
  queries.
- npub — NIP-19 bech32 pubkey encoding; accepted as input for `from`/`to`
  (any mix with hex), resolved to hex before querying.

## Out of scope

- `POST /shortestPath/batch` (deferred at kickoff 2026-07-13; see book).
- `via=` parameter / edge types beyond `FOLLOWS` (MUTES, REPORTS…).
- Undirected or mutual-degree variants.
- Precomputed `hops_<observer>` fast path.
- Path node hydration (names/avatars) — the UI hydrates separately.
- Other NIP-19 forms (`nprofile1…`, `nevent1…`, …) — only `npub1…` and hex
  are accepted.
- Rate limiting / abuse gating (issue defers until cost warrants).
- Auth: none required — public read, matching `/user/{pubkey}/*` (resolved at
  kickoff).

## Open questions

1. **Self-path semantics (AC4):** proposed `hops: 0` / `reachable: true`.
   → Resolves as proposed on story approval unless amended.
2. **Response envelope:** repo convention wraps success responses as
   `{code, data, message}` (see `app/routers/CLAUDE.md`), while the issue's
   example shows a bare object. Proposal: **follow the repo convention** —
   the issue's object becomes the `data` payload. → Resolves as proposed on
   story approval unless amended.
3. **400 vs 422 split (AC6):** issue says 400 for malformed input; FastAPI
   emits 422 for bounds/missing-param violations. Proposal: 400 for pubkey
   validation (explicit check), 422 for numeric bounds and missing params.
   → Resolves as proposed on story approval unless amended.
4. **Echo form with npub input (AC1):** proposal — responses always echo
   `from`/`to` as resolved hex (canonical, consistent with `path`), not the
   npub as given. → Resolves as proposed on story approval unless amended.

## Linked artifacts

- ADR: (filled in after Architecture phase)
- Test plan: (filled in after Test Design phase)
- Review: (filled in after Review phase)
