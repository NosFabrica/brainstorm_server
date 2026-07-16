# PRD Seed: Degree of Separation (follow-graph paths)

**Mode:** reconstructed from as-built *(no prior PRD)*
**Build audit:** `engineering-team/audits/shortest-path/audit.md`
**Anchor:** acceptance frame in `book.md`
**Confidence:** high
**Date:** 2026-07-13

> Reverse-engineered baseline in PRD shape, built from what shipped in the
> `shortest-path` book. A strawman for the product side, not a ratified spec.
> Tags: `[FROM FRAME]` — grounded in the kickoff acceptance frame / issue #43;
> `[INFERRED]` — read off the as-built system; `[UNKNOWN]` — needs product
> input.

## 1. Product vision

`[FROM FRAME]` Show any Brainstorm user how they're connected to anyone else
through the web of trust: a LinkedIn-style "1st / 2nd / 3rd degree" indicator
plus a "who connects us" chain, computed from real follow relationships rather
than an opaque algorithm. `[INFERRED]` The generic endpoint (decoupled from
`/user`) positions this as platform infrastructure any client can build on,
not just the Brainstorm UI.

## 2. Personas

- `[FROM FRAME]` **Brainstorm UI (profile page)** — knows both pubkeys (viewer
  + profile owner), wants a degree badge and a path preview.
- `[INFERRED]` **Third-party Nostr client / script authors** — public read +
  hex-or-npub inputs suggest an audience beyond the first-party UI.

## 3. Scope (as-built)

`[FROM FRAME]` Single-pair `GET /shortestPath`: directed `FOLLOWS`-only
traversal; reachable/hops/random representative path/exact path count with cap
flag; `maxHops` 1–50 (default 30), `maxPaths` 1–1000 (default 1000); hex or
npub inputs in any mix, hex-canonical echoes; self-path = 0 hops; public read;
input validation (400/422), no 5xx on any input.

Explicitly NOT in scope (deferred at kickoff or by issue #43): batch variant,
edge types beyond `FOLLOWS`, undirected/mutual degree, precomputed per-observer
hops, path hydration (names/avatars), rate limiting, auth gating.

## 4. Domain model

`[INFERRED]` `NostrUser` (node, keyed by hex pubkey) —`FOLLOWS`→ `NostrUser`
(directed; the only traversed relation). "Degree of separation from A to B" =
length of the shortest directed path A→B; asymmetric by design. Identity
formats: hex (canonical) and npub (accepted input, NIP-19).

## 5. Design rules (as-built)

- `[FROM FRAME]` Direction is always explicit (`from`/`to` named params) —
  never inferred from a resource path.
- `[INFERRED]` Success responses use the `{code, message, data}` envelope with
  camelCase keys; pubkeys in responses are always hex.
- `[INFERRED]` Unreachable is a *successful* answer (200 + `reachable: false`),
  not an error — clients render "no connection", they don't handle failures.
- `[UNKNOWN]` Error-response shape: currently FastAPI's `{"detail": …}`, which
  is the live API-wide convention but asymmetric with the success envelope —
  flagged by the operator at close (carry-forward).

## 6. Carry-forward & open questions

Promoted from audit §6:

1. **Batch endpoint** (`POST /shortestPath/batch`) — needed before list views
   can show degree badges at scale; design pre-agreed in issue #43.
2. **Error-envelope unification** — `ErrorResponseSchema` is documented as the
   convention but unused in live responses; three error shapes exist across
   the API. Cross-cutting; GitHub issue planned.
3. **Cypher timeout knob** — deferred with mechanism named (ADR 0001).
4. **flake8 PEP-701 policy** for Cypher f-strings (engineering hygiene).
5. Issue #43 future work: `via=` edge types, undirected variant, precomputed
   hops fast path, hydration.

## 7. What product must validate

- [ ] `[UNKNOWN]` When does the UI ship the degree badge, and does the batch
      endpoint (carry-forward #1) need to land first?
- [ ] `[UNKNOWN]` Abuse posture at public scale: the endpoint is open and
      graph traversals aren't free — revisit rate limiting once real traffic
      exists (issue #43 deferred it "until abuse/cost warrants").
- [ ] `[UNKNOWN]` Error-envelope decision (carry-forward #2): unify on
      `ErrorResponseSchema`, or ratify the FastAPI `detail` shape and update
      the docs that claim otherwise.
- [ ] `[INFERRED→validate]` Third-party consumption: is the generic public
      endpoint a product commitment (stable contract) or an internal detail?
