# OPEN — loose-ends ledger

Small / cross-cutting follow-ups with no other home yet. (Tapestry keeps this
at repo root; here it lives under `engineering-team/` to keep the root clean.)
Larger deferred scope lives in each book's audit §6 — link, don't duplicate.

| Opened | Item | From | Status |
|---|---|---|---|
| 2026-07-13 | File GitHub issue: `ErrorResponseSchema` documented as the error convention (`app/routers/CLAUDE.md`, `app/services/CLAUDE.md`) but never used in live responses; three error shapes exist. Unify via exception handlers or ratify reality + fix docs. | shortest-path audit §6 | pending |
| 2026-07-13 | Decide flake8 policy for PEP-701 E231 false-ish positives inside Cypher f-strings (py3.12 tokenizes f-string interiors). | shortest-path review non-blocking #1 | resolved 2026-08-26 — flake8 upgraded to ^7.1 (pycodestyle understands py3.12 f-string tokens); all 55 E231 false positives gone, no noqa needed |
| 2026-07-13 | Per-query Neo4j timeout knob (`neo4j.Query(timeout=…)`) if /shortestPath latency data ever warrants. | ADR 0001 Decision 2 | open |
| 2026-08-25 | Light-profile rubric bug: J2 item 5 cites tapestry `templates/adr.md` for the "guard-suite carve-out", but that file (40 lines) contains no occurrence of "guard" — and no `engineering-team/**` file in tapestry defines a guard-suite carve-out. The item is unjudgeable as written; a test-deliverable story would stall on it. Fix the pointer or drop the item. | trusted-lists J2 gate | open |
| 2026-08-25 | **Red gate baseline — blocks J3 for every story.** `poe check_all` fails on a clean tree at HEAD: `check_fmt` aborts the sequence with 38 files needing reformat, and behind it sit 52 isort, 129 flake8 and 92 mypy findings. The Light profile's "Instrument preconditions" asserts "This repo's pytest/`poe check_all` meets these today" — that is false, and J3 item 1 ("the full gate command … and green") is unsatisfiable until it's fixed. Per the profile, a red baseline is its own bug/refactor story. Note flake8's E231 count is inflated by the known py3.12 f-string tokenization issue (see the 2026-07-13 row). | trusted-lists Phase 4 | open |
