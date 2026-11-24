# Scorecards live in Postgres, keyed by User ids

Each Observer's Influence, Hops and Verified-follower count for an Observee were
three properties on that Observee's `NostrUser` node (`influence_<observer>`, …).
Neo4j's record format keeps a node's properties in one linked list and walks it
for any read or write, so every new Observer slowed every other Observer's reads,
every Scorecard write, and every read of `pubkey`. At about 1,580 Observers a
scored node carried about 4,500 properties, the property store outgrew the page
cache, the worker's reachable-users query went from minutes to 45 minutes, and
first-time writes failed on transaction memory.

Scorecards now live in a second Postgres instance, one row per Observer→Observee
pair:

```sql
nostr_user (id int PRIMARY KEY, pubkey text NOT NULL UNIQUE)
scorecard  (observer_id int, observee_id int, influence double precision, hops smallint,
            verified_followers int, PRIMARY KEY (observer_id, observee_id))
```

Neo4j keeps the graph: `NostrUser` nodes, now carrying `user_id` beside `pubkey`,
and the follow, mute, report, designation and zap-signer edges. A request that
needs both asks Neo4j who (as User ids) and Postgres for their Scorecards. User
ids are internal; the API and published events use pubkeys. Inside Brainstorm
the worker's result messages and the Redis relationship sets use User ids; job
messages name the Observer by pubkey.

Every writer of user nodes assigns User ids the same way: insert the missing
pubkeys into `nostr_user` and read all ids back, commit, then write the node with
`SET n.user_id = coalesce(n.user_id, $user_id)`. The unique constraint on
`pubkey` makes concurrent writers agree; an id is never overwritten or reused.

## Status

accepted (2026-11-23)

## Amended 2026-11-23

- Worker result messages and the Redis relationship sets use User ids from the cutover, not a
  later phase. Job messages still name the Observer by pubkey.
- The worker connects with the server's credentials; no read-only role.
- The whitelist is read from Scorecards with the cutover; its JSON copy is dropped after the
  Neo4j rebuild, since rolling back reads it until then.
- Rollback is turning the cutover setting off on the same images.

## Considered Options

- **Keep Scorecards on the user node** (the previous design): cost grows with
  every Observer for everyone. Bigger page caches and smaller write batches only
  buy time.
- **Sibling nodes in Neo4j, 16 Observers per shard**, found by an index on
  `(shard, pubkey)`. Built and reviewed on a branch that was not merged. With
  everything in memory it works. With 192M Scorecards and a quarter of them in
  memory, four readers and a writer: 1.2 requests a second against 13.3 for
  Postgres, 4.7 s against 0.5 s for a 6,000-row write, on equal disk. A sibling
  holds 16 Observers' values, so a read for one Observer pulls 16 times the data
  it needs.
- **The same shards keyed by integer ids**: a third of the pubkey-keyed store was
  the 64-character key repeated on every sibling. Ids made shards 36% smaller
  and about three times faster, the smallest layout on disk, and still 4 to 7
  times slower than Postgres on reads over 10,000 followers.
- **One node per Observer→Observee pair** with an index (measured separately in
  issue #140): within 2 times of Postgres where a read follows an edge, 2 to 7
  times slower where it does not, and about twice the bytes.
- **Postgres with text or 32-byte keys**: 399 and 234 bytes per Scorecard against
  75 with integer ids, for no speed.
- **Redis**: fastest in memory, and all of it has to stay in memory.

Further Scorecard fields decided it as much as speed: one more value on every
pair costs about 4 bytes in a Postgres row and 20 to 23 bytes as a Neo4j
property, and lengthens every sibling.

## Consequences

- A Scorecard read is two round trips. A failure between them fails the request.
- Sorting and filtering by Influence happen in Postgres over an id list from
  Neo4j. That grows with the size of the neighbour set (about 0.7 s for a sorted
  page at 330,000 followers, in memory). Walking the Observer's Scorecards in
  order and asking Neo4j who follows is faster above about 10,000 followers; it
  needs a second index and is not built yet.
- A user node without a `user_id` is invisible to Scorecard reads. Neo4j
  Community cannot require the property, so every service that creates user
  nodes (the server, graph-streamer, the profile resolver) must follow the id
  rule, and a nightly check counts nodes without one.
- The House Observer has no special case; its Scorecards are rows like any other.
- Verified-reporter and Verified-muter counts stay live: Neo4j returns the
  reporter ids, Postgres counts those above the Observer's current cutoff.
- The table is filled to 85% so a changed Scorecard is rewritten in place. Fill
  cannot change without rewriting the table. An index that contains Influence
  would end in-place rewrites.
- The table is not partitioned at first. Hash-partitioning by Observer was
  measured and gained nothing once the table is filled to 85%: the two fix the
  same thing. It is added later if speeds drop or maintenance gets long, by
  copying into a partitioned table with runs paused; no query changes.
- Writes use the default, durable commit. Relaxed commit
  (`synchronous_commit = off`) was considered and dropped: the writer commits
  once per run, so it saves one disk flush per run, and a crash would leave a
  run marked done and published with old Scorecards until that Observer's next
  run. It must never apply to `nostr_user`.
- Every shipped read names one Observer. A read across Observers for one
  Observee (rescoring a newly followed User for every eligible Observer, issue
  #141) is served by primary-key lookups over a named list of Observers, or
  needs a second index by Observee; that choice is made with that feature.
- Scorecards are recomputable and are not backed up. `nostr_user` is.
- The whitelist (an Observer's Valid users with their Influence) becomes a read
  over Scorecards at the cutover; its separate JSON copy is dropped after the
  Neo4j rebuild.
- Size grows in a straight line: about 21 GB per 1,000 Observers at 286,000
  Observees each. Because reads name one Observer, the table can later be split
  across instances without cross-instance queries.
- The cutover is one setting, turned on for server and worker together and
  off again to roll back, on the same images. Neo4j's old properties are left in
  place until the store is rebuilt without them, which is what makes rolling back possible,
  and also means pubkey reads from heavy nodes stay slow until that rebuild.
- The worker gains a Postgres connection, with the Scorecard database's user
  over read-only connections. Its result messages name users by User id; its
  job messages are unchanged.
