# Intake log

Append-only. Raw requests, classification, and chosen phase path.
Protocol: run per tapestry's `engineering-team/` harness (workflows followed from
`~/repos/nous-clawds4/tapestry/engineering-team/`, artifacts written here —
docs-only, no harness port). Strictness: **Standard**. Gates: **interactive**.

---

## 2026-07-13 — shortest-path (follow-graph) API endpoint

**Raw request (David):** "I would like to build the feature filed here:
https://github.com/NosFabrica/brainstorm_server/issues/43" … "can we build it
using the engineering team protocol that is described in the tapestry
repository (at `repos/nous-clawds4/tapestry`)?"

**Source spec:** GitHub issue #43 "Add generic shortest-path (follow-graph) API
endpoint" (author: nous-clawds4) — full endpoint design, semantics, Cypher
approach, guardrails, and v1 scope checklist are in the issue.

**Classification:** Feature (new behavior).

**Strictness path (Standard):** all phases — Planning → Architecture →
Test Design → Implementation → Review; book close at the end.

**Book:** new, no PRD → acceptance frame captured in
`engineering-team/audits/shortest-path/book.md`.

**Epic:** `shortest-path` (new).

**Environment note:** local full stack is running (see
`brainstorm_one_click_deployment/docker-compose.dev-ports.yml`); Neo4j at
bolt://localhost:7688 with a live-accumulated FOLLOWS graph for integration
testing.

**Kickoff amendment (2026-07-13, David):** v1 scope narrowed to the single
`GET /shortestPath` endpoint — the batch variant from issue #43 is deferred
(recorded as out-of-scope in the book's acceptance frame). Auth posture
resolved: public read. Frame + path confirmed.
