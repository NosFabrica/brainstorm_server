# Epic: shortest-path

**Status:** Active
**Book:** `engineering-team/audits/shortest-path/book.md`
**Source:** GitHub issue #43 — https://github.com/NosFabrica/brainstorm_server/issues/43

Generic shortest-path query over the directed Neo4j `FOLLOWS` graph, exposed as
a public read API. v1 is the single-pair endpoint only (`GET /shortestPath`);
the batch variant was deferred at kickoff (2026-07-13).

## Stories
- `stories/shortest-path/1-get-shortest-path.md` — the single-pair endpoint.
