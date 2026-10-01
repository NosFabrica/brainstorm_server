# /shortestPath returns the whole Path network, with no randomness and no cap

`GET /shortestPath` returns the complete **Path network** from `from` to `to`.
That is every Connector on any shortest Path, grouped by hop (`layers`), the
follows between neighbouring layers (`links`), and the exact `pathCount`. The
Connection page uses it to find Paths through Flagged or Unverified Connectors,
and an answer that leaves any out is wrong. A Path network grows with the
number of accounts involved, not with the number of Paths. On staging the worst
pair seen (1,127 Paths) is 318 accounts, roughly 10× smaller than the same
answer sent as a list of Paths.

This replaces the random-representative-path design in
`engineering-team/decisions/shortest-path/0001-shortest-path-query-and-placement.md`.
That design returned one random Path and capped `pathCount` at `maxPaths`.
`path`, `pathCountCapped` and `maxPaths` are removed; there is no backwards
compatibility, because the UI was the only caller and changes in the same release.

## Status

accepted

## Considered Options

- **One random Path** (the previous design): the UI had to sample by calling
  repeatedly, and missed Flagged Connectors by chance.
- **A capped list of Paths**: any cap can drop the one Path through a
  Flagged Connector. Choosing which Paths to keep by risk would need each
  viewer's trust data on the server.
- **An uncapped list of Paths**: complete, but the response multiplies with
  every hop.

## Consequences

- No size cap. The only guard is a 5 s query timeout, which returns **504**,
  never a partial network. The UI falls back to hops only.
- A follow-up adds `only=hops`, which answers Hops alone from a single
  shortest-path lookup. It's meant for the profile degree chip, which needs only
  Hops on every profile view, and it reads live follows, so it agrees with the
  Connection page. The stored per-Observer Hops snapshot was
  rejected for this because it is only as fresh as the last GrapeRank run.
- Each layer is sorted by pubkey, so the same input gives the same bytes. Display
  order (safest Path first) is the UI's job.
