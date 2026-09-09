# OPEN — loose-ends ledger

Small / cross-cutting follow-ups with no other home yet. (Tapestry keeps this
at repo root; here it lives under `engineering-team/` to keep the root clean.)
Larger deferred scope lives in each book's audit §6 — link, don't duplicate.

| Opened | Item | From | Status |
|---|---|---|---|
| 2026-07-13 | File GitHub issue: `ErrorResponseSchema` documented as the error convention (`app/routers/CLAUDE.md`, `app/services/CLAUDE.md`) but never used in live responses; three error shapes exist. Unify via exception handlers or ratify reality + fix docs. | shortest-path audit §6 | pending |
| 2026-07-13 | Decide flake8 policy for PEP-701 E231 false-ish positives inside Cypher f-strings (py3.12 tokenizes f-string interiors). | shortest-path review non-blocking #1 | resolved 2026-08-26 — flake8 upgraded to ^7.1 (pycodestyle understands py3.12 f-string tokens); all 55 E231 false positives gone, no noqa needed |
| 2026-07-13 | Per-query Neo4j timeout knob (`neo4j.Query(timeout=…)`) if /shortestPath latency data ever warrants. | ADR 0001 Decision 2 | open |
