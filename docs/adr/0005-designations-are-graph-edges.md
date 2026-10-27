# kind-10040 designations are graph edges every Observer's GrapeRank reads

A kind-10040 names the keys its author trusts to publish Trusted Assertions for
them. Until now only the author's own run used it: the 10040 was read at enqueue
time and rode on that one calc message as `designated_pubkeys`, pinning the keys
at Influence 0.95. Nobody else's run saw it, so a provider key trusted by half an
Observer's network was invisible to that Observer.

Designations are now stored like follows. A 10040 becomes `DESIGNATES` edges in
Neo4j and a forward Redis set `designates:<author>` (`process_event_kind_10040`).
The GrapeRank worker reads `designates:` for every user it reaches, adds the
designated keys to the run even when no follow reaches them, and scores each
designation as a rating:

- **the Observer's own** designations stay pinned at 0.95 (rank 95);
- **anyone else's** rates the key at 0.95 with follow confidence, weighted by the
  designator's Influence, exactly as a follow is, at a 0.95 rating instead of 1.0.

## Status

accepted

## Considered Options

- **Keep the enqueue-time read only** (the previous design): one Observer sees
  only their own designations; the graph never learns who trusts which provider.
- **A reverse set (`designated_by:<key>`)**, like `followed_by:`: the worker reads
  incoming edges per ratee. A provider key is designated by many and designates
  nobody, so its reverse set is a hub read in full on every run that reaches it.
  Forward sets cost one read per reachable user, bounded by the run's own size,
  and they find the designated keys the follow BFS never reaches, which a reverse
  read per ratee cannot (a key must already be in the run to be asked about).
- **A designation is a follow** (rating 1.0, no new kind): no worker change to the
  rating tables, but it would also count toward trusted followers, and a provider
  key is not someone's follower graph.
- **Designation rating/confidence as preset params**: tunable per Observer, but a
  migration plus a Java-then-Python deploy for a value nobody has asked to tune.
  Constants first; promote them when someone does.

## Consequences

- Designations reach one level: a key reached only by designation does not have
  its own designations read. Provider keys designate nobody in practice.
- A follow and a designation of the same key from the same user are two ratings.
- Designation edges add no hops: hops stay follow-only, so a key reached only by
  designation is published with hops `UNREACHABLE_HOPS` (999).
- Designations never count as trusted followers, muters or reporters.
- Only public designation rows count; entries encrypted into the content need the
  author's key.
- Existing 10040s are loaded once with `scripts/backfill_designations.py` from
  neofry. Live ingest needs neofry's strfry plugin to forward kind 10040 onto
  `strfry:events` (brainstorm-k8s config).
- `designated_pubkeys` on the calc message stays during the transition; the worker
  unions it with the Observer's `designates:` set. Drop it, and the enqueue-time
  read in `designation_service`, once the backfill has run in every environment.
- Deploy order: either. An old worker ignores `designates:`; a new worker with no
  `designates:` data behaves as before.
- The NIP-57 zap provider key gets the same treatment (`ZAPS_VIA` edges + `zaps_via:` sets), written by `NosFabrica/brainstorm_profile_resolver`, not this server.
