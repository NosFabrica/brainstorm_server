# ADR 0001: Your Assistant's Treasure Map lists what Brainstorm's records show is kept up to date, read against a per-key offer

**Status:** Proposed. Written after the build, on draft PR
[NosFabrica/brainstorm_server#110](https://github.com/NosFabrica/brainstorm_server/pull/110) (commit `e2ca872`). No
Gate A was held. It's for David to accept and for the Gate B reviewer to ratify, together with this ADR's
classification. Revised 2026-10-28 for the PR 110 audit: D2 (follows), D3 (when the choice is saved), D4 (no merge,
the writer's relays only, the repair, the 64-key cap, Turn off without the follow count), D5 (refuse, not queue), D6,
D7 and the Debt. Revised 2026-10-29 for the audit's second round: D4 (the follow count bounded and read only when it
counts; Turn off's fallbacks, which the UI couldn't send), D5 (the time a press takes), D6 (the UI's rule, and the
repair it couldn't send), D7, the Debt and Out of scope (when the UI's fix must reach production). Revised again for
its third round: D4 (the follow count read alongside the caller's Map, and the Map read once), D5 (28 s), D7 (the
follow count's 502) and the implementation notes (`_follow_count`), and D4, D6 and the Debt for
NosFabrica/Brainstorm-UI#307, which offers the repair and Turn off while the GET fails. Revised for its fourth round:
D4 (Turn off reads no caller's Map; when the follow count counts; what the cap's check costs), D5 (Turn off's 22 s),
D6 and D7 (`writerRelays`, the relays read by the press that keeps the current choice, David's decisions), the Debt
and the implementation notes, and the Debt and Out of scope for NosFabrica/Brainstorm-UI#306, which took the UI's fix
to production.
**Date:** 2026-10-27
**Story:** [`stories/assistant-treasure-map/1-treasure-map.md`](../../stories/assistant-treasure-map/1-treasure-map.md),
written after the build (2026-10-28)
**Ask:** Brainstorm-UI `docs/assistant/TREASURE-MAP-ASK.md`, corrected by NosFabrica/Brainstorm-UI#301; the UI is
NosFabrica/Brainstorm-UI#297

## Context

An Assistant's **Treasure Map** is its own kind 10040, naming itself only for the duties it publishes now and keeps
up to date: a *self-Map* (Tapestry's `protocols/drafts/treasure-maps.md` § 9.2; the § numbers below are its). Each
self-entry `[key, A, relay]` says that A publishes at that relay, and keeps up to date, the insights the key wins.
The Duty Menu says what the Assistant offers (ADR `assistant-duty-menu/0001`); this says what is active. A client
that sees a self-entry can switch to that Assistant at once (§ 9.2 "Confirmed duties").

Brainstorm-UI's fifth Account Setup step (`/setup/treasure-map`) shows three things: what the caller's Map asks of
their Assistant and what of it is live, what the Assistant's Treasure Map should list, and the version on relays. It
has Tapestry's Turn on, Update now and Turn off. Only the server holds the key and knows what is live, so it decides
what to list and signs it.

Under the Light profile (`../../workflows/light-profile.md` § Gate A), this change trips three irreversibility
triggers:
- a **wire format**: the self-Map event, and the two routes' bodies;
- a **schema and migration**: `brainstorm_nsec.treasure_map_on`;
- a **cross-repo contract**: the UI's `parseTreasureMapCheck` and `TreasureMapPublishResult` read the answers.

So it owes a full ADR. This is that ADR, recording what PR 110 did.

### What Brainstorm adopts from Tapestry

`app/services/self_map_build.py` is a port of Tapestry's `src/lib/self-map`, with these decisions kept as Tapestry
made them:
- **The meaning of a self-entry**, how it is found, and when it may be added, kept or must go (ADRs
  `duty-menus-and-self-maps/0003` and `0005`), over the key grammar of ADR `duty-menus-and-self-maps/0004`.
- **A bounded engine** (ADR `assistant-self-map/0001` K1). It reads:
  - kind slots;
  - Score metric keys;
  - Concept keys.

  It reasons over a finite set of probe insights. Any other key shape is *unchecked*: never claimed, and, as a duty,
  it keeps a broader self-entry from winning it.
- **The should-say rule** (ADR `assistant-self-map/0001` sub-decision 2). It is greedy, most specific first:
  - each of the caller's keys naming the Assistant is tried verbatim;
  - then its meets with each offered kind it covers;
  - then, for a `30382` meet, one `30382:<metric>` per published metric.

  A candidate is valid at relay R when every duty it would win is live at R, and it wins at least one. Nothing else
  is copied from the caller's Map (§ 9.2 "No order, no copies").
- **The published metrics as the supported Scores** (ADR `assistant-self-map/0001`, open question 1, as answered).
  Here those are `ta_signing.TA_METRIC_TAGS`, the five `ta_tags` writes.
- **One allowance of two intervals.** One missed run is a brief gap; two in a row is a stop.
- **One route with a strict boolean body**, where Turn on and Update now are both `{on: true}` (ADR
  `assistant-self-map/0002` P1). **The strict read** (R1 there), for a reason of its own here (D4).

**Not adopted: the writer's merge** (ADR `assistant-self-map/0002` sub-decision 4 and Amendment 1 item 2: every other
tag and the content kept, the owned tags, the `expiration` rule) and its preservation check. Here nothing but this
server signs the Assistant's kind 10040, so there is nothing to keep (D4).
- **The line keys of a part** avoid § 4.4's Profiles spelling (Tapestry ledger
  `2026-10-12-self-map-profiles-category-lines`).

### Repo facts this ADR builds on

- The Duty Menu's offer is per key: five `30382:<metric>` rows, then `30392`, `39998`, `39999`
  (`duty_menu_build.offer_rows`, ADR `assistant-duty-menu/0001` D3).
- `brainstorm_nsec.last_time_published_graperank` is advanced only after the relay OK'd every event of a run.
  `upload_nostr_events` fails the run on any unacked event, before the clock moves.
- Steady-state publishing is **delta** (`relay_full_sync` false). A run signs only the scores that changed, so a
  healthy run with nothing changed signs no TA, yet still advances the clock.
- The scheduler admits a customer by these inputs:
  - `settings.scheduler_enabled`;
  - a default scheduling policy (it skips every cycle without one);
  - the customer's policy, or the default, with its `enabled` and `schedule_interval_seconds`;
  - a grapevine: it skips a customer who follows no one (`cronjobs.scheduler.has_grapevine`).

  It excludes the House Observer, which `periodic_graperank_trigger` re-runs every `PERIOD_HOURS` (2).
- Trusted Lists are published only when an admin runs `POST /admin/trustedLists`. The Assistant publishes no Concepts
  yet; that's [NosFabrica/brainstorm_server#112](https://github.com/NosFabrica/brainstorm_server/issues/112).
- The caller's own kind 10040 is on our relay (`nostr_transfer_to_relay`), filled by neofry's `designations` stream.
  `designation_service` reads it.
- The server runs one replica: `server.replicas: 1` in brainstorm-k8s `charts/brainstorm/values.yaml`, not overridden
  in `prod-values.yaml` or `staging-values.yaml`. `start.sh` runs one uvicorn worker.

## Decisions

### D1 — The offer is read per key, by Duty Menus § 6.2 covering

`Offer` in `self_map_build.py` gives:
- a **probe** the relay of its most specific matching row (§ 6.2 step 2);
- a **candidate self-entry key** the relay of the most specific row *covering* it (§ 6.2 steps 1–3). A key that no
  row covers is never a self-entry key.

**Options considered:**
- **A (chosen): covering, per key.** A bare `30382`, `3038x` or `*` narrows to the five metric keys. Another `30382`
  metric, and `30383`…, are `not-offered`.
- **B: Tapestry's reading**, bare-kind `duty` rows only (`evaluateSelfMap` keeps `/^\d{5}$/` keys). **Rejected:**
  Brainstorm's offer has no bare `30382` row, so Scores would read as not offered and the Map would list nothing.
- **C: § 6.2 step 5's insisted-key relay** for a key no row covers. `30382` would get the scores relay, since every
  row within it names that relay, and § 9.2 "Its relay" would allow it. **Rejected:**
  - A reader takes a self-entry to speak for every insight it wins, unless it knows otherwise (§ 9.2 "What a reader
    can tell"). Brainstorm publishes five metrics, not every `30382`.
  - "A self-Map says less rather than more."
  - Tapestry's own note says to narrow to `30382:<metric>` keys once the supported Scores name a metric not computed
    (ledger `2026-10-12-self-map-upkeep-cross-book-notes` item 3). Brainstorm's offer is that case from the start.

**Consequence accepted:** a reader whose Map is `/setup`'s rows gets exactly the five Score rows, entry for entry. A
reader who gave a bare `30382` gets the same five, never `30382`. One whose Map is what Brainstorm-UI's activation
writes (`30382:rank` and `30382:followers`, and `30392`–`30394` from `/setup`'s `30392` row) gets those two Score
rows; `30393` and `30394` read as not offered.

### D2 — "Live" comes from Brainstorm's own records, not from reading the relay

Scores are **live** when all of these hold:
- `last_time_published_graperank` is set;
- something keeps them up to date: the scheduler's inputs above, follows included, or, for the House Observer, the
  periodic trigger's 2 hours;
- the last publish is no older than two of those intervals.

Otherwise the reason is:
- `not-scheduled` when nothing keeps them up to date (Tapestry's order: before `not-landed`);
- `not-landed` when they have never been published;
- `upkeep-stopped` when they have stopped, with `lastLandedAt`, the last publish in seconds.

Trusted Lists are `not-scheduled`. Concepts are `not-published-here`. Neither is ever listed.

**Options considered:**
- **E1: Tapestry's reading** (ADR `assistant-self-map/0001` E1): the newest Assistant-signed 30382 at the relay, as a
  heartbeat. **Rejected:**
  - Under delta publishing, that newest TA is no heartbeat. A quiet, healthy observer whose scores haven't changed for
    weeks would read `upkeep-stopped`.
  - It also costs a relay read on every check.
- **E2 (chosen): the publish clock and the scheduler's own inputs.** Tapestry rejected its run records (its E2)
  because failures read as success there. Here the clock moves only when every event was OK'd, so that objection
  doesn't hold. The check reads the scheduler's standing inputs, and its follows rule through the same helper, so
  the two agree on whether anything keeps the Scores up to date. It doesn't read what only delays a run: one in
  flight, the retry backoff after a failure, the admission budget. Those the two intervals absorb. The first build
  missed the follows rule, so a customer who followed no one read as live (the PR 110 audit).
- **E3: new upkeep stamps written by the publishers.** Not needed: E2's clock already is one.

**Consequences accepted:**
- **The self-Map trusts the server's own record of what landed.** A relay that later loses events isn't noticed
  until the next full run (`full_sync_every_n_runs`) re-asserts them.
- The House Observer special case follows `PERIOD_HOURS`, imported from the cronjob, so the two can't drift.

### D3 — The choice is a column, `brainstorm_nsec.treasure_map_on`

It's a boolean, default false, added by migration `c5f1d7a9e2b4`. Only `POST /user/assistantTreasureMap` writes it,
at a press's success exits, after the publish, and commits it before answering (`self_map_service._save_choice`). A
press that answers an error leaves it as it was. Written before the publish, as first built, it kept the caller's
`brainstorm_nsec` row locked through the sign, the connect and the send. Left to `get_db`, which commits only after
the response, a failed commit would have followed a 200.

**Options considered:**
- **A (chosen): a column on the Assistant's row.** That row already exists for every Assistant and holds its key. It
  is migrated and backed up like everything else, and its write commits with the press or not at all.
- **B: a table of its own.** **Rejected for now:** a new table for one bit, on a row that already exists per
  Assistant. Worth revisiting when a second per-Assistant choice appears.
- **C: a Redis key.** **Rejected:** Redis here is a queue and a cache (rate-limit counters, the reverse-set caches),
  not a store of record. A choice must survive a flush and be in backups.
- **D: Tapestry's file on a data volume** (ADR `assistant-self-map/0002` C1). **Rejected:** the server keeps no data
  volume. Its state of record is PostgreSQL, so C1's reasons for a file (surviving relay rebuilds and firmware
  reinstalls) point to the database here.

### D4 — A press reads strictly, publishes the self-entries alone, and goes only where § 9.2 sends a writer

**The reads:**
- The caller's Map is read from our relay, through `designation_service.fetch_latest_designation`, strictly, and only
  for Turn on: 502 when it can't be read.
- **Turn off reads neither the caller's Map nor the follow count.** It lists nothing, and under today's offer neither
  could change where it reads or sends: without the follow count, Scores read as not scheduled, and only Scores can
  be live, so its `shouldSay` was empty for every Map; live Scores would only add the TA relay, which the outbox
  names first. Read anyway, as built until the audit's fourth round, the Map cost Turn off up to 6 s, and was the only
  reason it had fallbacks (no Map, more than 64 keys). A press now reads where the version it publishes is looked for
  (`_writer_relays` of what it lists), so Turn off reads the outbox alone, and sends there and to every relay an
  earlier version's self-entries named. Should a duty other than Scores become live at a relay the outbox doesn't
  name, a press that withdraws it there, a Turn off or an Update now that drops it, would send that relay the new
  version once; if the relay missed it, no later press would read or send there again (Debt).
- A Map naming more than 64 keys (`self_map_build.MAX_MAP_KEYS`) is a 422 for the GET and for Turn on, and is never
  checked. A check weighs each key against every other, so its cost grows with the square of the keys: an 835-key
  Map took 5.6 s of the event loop's CPU per GET. It is never truncated, which could give the wrong self-entries.
  `/setup` writes eight keys. The Map is read once per request (`map_groups`). On an M3 Ultra, 64 short keys naming
  the Assistant and 12,000 others (0.9 MiB) cost a request about 65 ms, and long keys and many hints cost more: 64
  keys of 16,000 segments about 4.5x, 10,500 hints for the Assistant about 2.2x. The audit's M1 takes 1.25–1.6x as
  long.
- The follow count (D2) counts only when the caller's Map asks the Assistant for Scores: a key naming it covers kind
  30382 (`self_map_build.asks_for_scores`). Otherwise no Score is its duty, and the upkeep changes nothing the check
  says. The GET and Turn on read it alongside the caller's Map, within 5 s, and await it only when it counts and the
  scheduler would otherwise run the caller: after the House Observer's, `scheduler_enabled`'s and the policies'
  answers (`_upkeep_interval`). Then it's strict: a 502 when it failed or wasn't read in time, because read as none
  it would take the Scores entries off. Otherwise it's cancelled, and awaited, so the read has stopped, its Neo4j
  session released, before the answer goes out. Neo4j being down stops no one whose Map asks for no Scores.
  - Bounded: the Neo4j driver has no query timeout and waits 30 s to connect, so unbounded, a press outlasted the
    UI's 30 s, holding its slot (D5), and then published. The bound runs inside the read, from its start: at the
    await, it would add its 5 s to a press.
  - Alongside the Map: the second round read it after the Map, within 1 s, to keep a press inside the UI's 30 s. A
    stall of this process's event loop, which the GrapeRank result consumers share, could then fail an answer
    Neo4j gave at once. Read alongside, and bounded above a second and under the Map read's 6 s, it adds nothing
    to a press.
- **Turn off while the GET fails.** Turn off reads neither, so it goes ahead in the states where the GET answers 422
  or 502 for the Map or the follow count. NosFabrica/Brainstorm-UI#307 (open) offers Turn off beneath that error.
  Until it lands, the UI shows no buttons until a GET succeeds, and that Turn off goes through the API alone (Debt).
- Then the writer's relays (below) are asked for the Assistant's kind 10040, the TA relay at its internal URL:
  - only versions validly signed by the Assistant count;
  - the newest by NIP-01 is the one compared and dated after;
  - 502 when no relay answers.
- The reader is the plain REQ-until-EOSE, which raises on `CLOSED`, so a refusal never reads as "none":
  - nostr-sdk can't tell a silent relay from an empty one; both give zero events and no error.
  - Its 5 s deadline covers the answer, not the close, which is bounded at 1 s.

**Options considered:**
- **R1 (chosen): strict.** A version left unread could be dated ahead of now, so the new one wouldn't replace it, or
  name relays the press would then miss.
- **R2: best-effort.** **Rejected** for that reason. The Duty Menu now reads strictly too (ADR
  `assistant-duty-menu/0001` D5).

**No merge.** A version's tags are its self-entries and our `client` tag, and its content is empty
(`self_map_build.self_map_tags`). Tapestry keeps every other tag and the content of the version found, because other
writers sign its Assistant's kind 10040. Here nothing else does: no route exports the Assistant's key, and nothing
else in this server signs that kind. So the kept tags would always be none. Should the key ever be exported,
restore Tapestry's merge (ADR `assistant-self-map/0002` sub-decision 4) and its preservation check. "Nothing to
change" is the self-entries alone: the newest version's already are the target. The new version is dated
`max(now, newest version found + 1)` (§ 9.2 "When it changes").

**The check before sending** (`assistant_publish.sign_checked`, shared with the Duty Menu): the signed event
verifies, its tags are exactly what was built, and its self-entries read back as the target. If not, the press is a
500.

**Where it goes:** what § 9.2 "Finding a self-Map" gives a writer:
- the Assistant's outbox (kind-10002 write relays, `assistant_relay_list()`);
- every relay its self-entries name;
- every relay a self-entry of a version found named.

Every one comes from settings or from a version the Assistant signed. The caller's hints for the Assistant (element
3 of their entries naming it) are in the GET's `relays`, for readers, as § 9.2 tells readers to look there, but a
press never connects to them.

**Options considered:**
- **W1 (chosen): the writer's relays only**, as above.
- **W2: also the caller's hints**, as first built, because Brainstorm-UI's `TREASURE-MAP-ASK.md` asked for them.
  **Rejected** (the PR 110 audit): the caller writes their own Map, so a hint can name any host, internal ones
  included, any number of times. Every press dialled them twice, with no cap, and answered the transport's errors.
  It also sent every version to the Dictionary relay, a hint from `/setup`'s Concept rows, which refuses a kind
  10040, and counted that relay's EOSE as a finished read.
- **W3: Tapestry's**, hints too, capped at 50 and kept to public addresses (`selfMap.js`, `MAX_RELAYS` and
  `isPublicRelayHostWithin`). **Not adopted:** § 9.2 doesn't ask a writer to send there. A public-address guard has
  to resolve each host and pin the address it checked, or a rebinding name gets past it.

**When nothing would change, a relay that missed the newest version gets it.** The read keeps each relay's answer.
A press whose target is already the newest version's self-entries sends that version, already signed and unchanged,
to every relay that answered the read without it: with none, or with another version. It answers `published: true`
with that event and those relays' report, even when none of them takes it: the version is already on the relay it
was read from, and the report shows each failed line. A relay that didn't answer is left alone: it is likely down,
and would otherwise be sent to, and reported, on every press. So the press after one whose TA relay was down repairs
it; as first built, every later press answered "nothing changed". Only when every relay holds it is the answer
`published: false`. The GET names the relays read by the press that keeps the current choice (`writerRelays`, D6),
so the UI can ask each one, and NosFabrica/Brainstorm-UI#307 (open) offers that press.

Signing and sending are `assistant_publish`'s, shared with the Duty Menu. Tapestry left that sharing for later (its
S2) because its Duty Menu route had shipped. Neither route had shipped here.

### D5 — One press at a time per Assistant, in this process; a second is refused

**Options considered:**
- **A (chosen): refuse a second press**, a 409 with a plain sentence while one for the same Assistant is under way.
  `self_map_service` keeps the set of Assistants with a press under way, and an Assistant leaves it when its press
  ends, errors included. The server runs one replica with one worker (see the repo facts), so it holds today.
- **B: queue it behind an `asyncio.Lock`**, as first built. **Rejected** (the PR 110 audit): a queued press answers
  only once both have run, up to ~50 s with a slow relay and past the UI's 30 s, and still publishes. It holds a
  pooled DB connection while it waits, and a burst of them ties up the pool the scheduler and the MQ consumers share.
- **C: a Redis lock**, as `leader_lock`. **Deferred:** it's needed only if the server scales out, and the module's
  comment says so.
- **D: no lock.** **Rejected:** two presses at once read the same version and sign two with the same `created_at`.
  NIP-01 keeps the lower id, so a Turn off racing a Turn on would leave either one (§ 9.2 "When it changes"). A test
  runs two at once.

A press takes about 28 s of the UI's 30 at worst: the caller's Map and the versions are read in up to 6 s each, the
connect takes up to 4 s and the send 12 s. The follow count is read alongside the caller's Map, within 5 s (D4), so it
adds nothing. Turn off reads neither, and takes about 22 s. `test_a_press_fits_in_the_uis_30_seconds` sums these
bounds, and holds the follow count's between 1 s and the Map read's.

### D6 — The server answers what to list; the UI reads the published version itself

The GET answers `{assistant, on, duties, shouldSay, relays, writerRelays}`. Its only relay read is our own relay, for
the caller's Map. The UI reads the Assistant's kind 10040 from `relays` and compares it with `shouldSay`, as a set of
`(key, relay)`, as it does for the Duty Menu. Its Nostr reads come from its EventStore (Brainstorm-UI ADR 0005).
`writerRelays` names the relays read by the press that keeps the current choice, each once, at public URLs, and never
a hint (below): while on, Update now's (the outbox, then the relays `shouldSay` names); while off, Turn off's (the
outbox alone).

**Options considered:**
- **A (chosen):** as above. The answer carries no `published`, `current`, `falseClaim` or `active`; the UI works them
  out. `duties` lines are `live`, `offered` (with a `reason`), `not-offered` or `elsewhere`. `entries` lists the
  `shouldSay` entries that list a live line.
- **B: the found version rides the answer**, as Tapestry's ADR `assistant-self-map/0003`. **Rejected:**
  - every page load would cost an outside relay round;
  - it would add a second, server-side copy of a Nostr read the UI already holds and keeps current.

**The cost:** the UI reads a version as the press does, so the two must agree on when nothing would change. That is
a cross-repo contract. NosFabrica/Brainstorm-UI#301 aligned the UI with `nothing_to_change` as first built: the kind
slots the writer owns, each key and relay once, and the `expiration` and stray-tag rule. The server's rule is now the
self-entries alone (D4), and NosFabrica/Brainstorm-UI#302 made the UI's the same: its `selfMapStatus` compares the
self-entries alone, and `nothingStale` is gone.

**What they don't share: the repair** (D4). The UI reads the newest version found on any of the relays it's given,
so that alone can't show a relay that missed it: after such a press, the Map reads up to date. A press repairs only
the relays it reads, so the GET names the relays of the press the UI offers for it, `writerRelays`, and the UI can
ask each one, by id, whether it holds the newest version. `relays` can't serve: beyond the TA relay and the relays
`shouldSay` names, it holds the outbox's other relays and the caller's hints, which it doesn't tell apart, and a
hint must never be asked, since a press never goes there. David chose the GET naming them over the UI working them
out of `relays`, and chose the press that keeps the current choice (2026-10-29): Update now while on, Turn off while
off. Update now's set, probed while off, would name a relay only `shouldSay` names, and offer a Turn off that never
mends it, since Turn off reads the outbox alone. Today the two sets are the same, because `shouldSay` names only the
TA relay, which the outbox names first; they differ once a duty can be live at a relay the outbox doesn't name.

NosFabrica/Brainstorm-UI#307 (open) asks the relays it can tell apart in `relays` (the TA relay and, while on, those
`shouldSay` names), names one that answers without the newest version, and keeps Update now enabled, or while off
Turn off offered, so the press repairs it. Until it lands, that press goes through the API alone. Its probe is to
move to `writerRelays`, which closes its limit. Until then, a version missed only by an outbox relay other than the TA
relay waits for the next press. When that version withdraws a claim (a Turn off, or an Update that drops a duty),
the relay keeps serving the withdrawn self-entries meanwhile, and the UI offers no press that repairs it.

### D7 — The wire: camelCase, plain sentences, nothing changed on an error

- **camelCase** (`shouldSay`, `writerRelays`, `lastLandedAt`), as the UI's parser reads it, through Pydantic
  serialization aliases. The parser ignores a field it doesn't know, so `writerRelays` changed nothing for the UI on
  staging.
  This departs from the repo's snake_case for new endpoints (`app/schemas/CLAUDE.md`), because the UI's contract was
  written first and is on staging.
- **Wrapped in `{data}`**, like every `/user` answer.
- **On the authenticated `/user` router**, as the Duty Menu's (its D7).
- **The body** is `{on: StrictBool}`; anything else is a 422.
- **Errors are plain sentences** in `detail` (`app/services/CLAUDE.md` § Errors):
  - 502 when the caller's Map can't be read or, when it counts (D4), the follow count (the GET, and Turn on), when
    no relay answers the read, or when no relay takes a new version;
  - 422 when the Map names more than 64 keys (the GET, and Turn on);
  - 409 while another press for the same Assistant is under way;
  - 500 when the signed event fails its own check.
- **`reason` uses only the words the UI has:** `not-landed`, `not-scheduled`, `upkeep-stopped`,
  `not-published-here`, `nothing-to-publish`, `unchecked`.
- **Nothing to change** answers `{on, published: false, event: null, relays: []}` when every relay that answered the
  read holds the newest version. When one doesn't, the press sends it there and answers
  `{on, published: true, event, relays}`, with that version and those relays, whether or not they take it (D4).
- **A relay that couldn't be reached** is reported as `couldn't connect`. The transport's own error is logged, not
  answered: it can describe a host the caller can't see.

## Consequences

**Enables**
- The UI's fifth setup step, with Tapestry's buttons.
- A client that sees the self-entries can take the customer's Scores from this Assistant at once, and count them as
  maintained at the Assistant's relay (§ 9.2 "Confirmed duties", "Maintained").
- The Duty Menu and the Treasure Map share one offer and one way to sign and send (`assistant_publish`).

**Constrains / makes harder**
- **The self-Map is only as true as the publish clock and the scheduling policies.** A change to how either works
  changes what it claims.
- **Scores are listed as five entries, not one** (D1), and the published event is the self-entries plus `client`.
- **When nothing would change is now a contract with Brainstorm-UI** (D6), and where a version goes one with
  Tapestry's readers, through § 9.2.

**Debt and follow-ups**
- **It doesn't keep itself true.** § 9.2 says a key MUST remove or narrow a self-entry before it stops keeping it up
  to date. Until something republishes when the answer changes, the UI shows the Map out of date and the customer
  presses Update now. Natural hooks are a successful TA publish, a policy disabled, or an entitlement lapsing. This is
  Tapestry's story 5, not built there either.
- **A relay that refused a removal keeps serving the old claim.** Each later press that reads there sends it the
  newest version again (D4) and reports the refusal. A relay only an earlier version named, which no press reads, is
  sent the removal once and never again (the Trusted Lists bullet below). Tapestry's ledger row
  `2026-10-13-self-map-refused-removal-stays-outside` is the same case.
- **Brainstorm-UI read a self-Map as an activation; done.** Its `describeDesignation` described any kind 10040 whose
  providers are all on Brainstorm relays as "Activated Brainstorm trust signals", and the Assistant's own self-Map is
  one. NosFabrica/Brainstorm-UI#302 fixed it, and the fix had to reach UI production before any server published a
  self-Map, staging's included: an Assistant's outbox names public relays, so a staging Assistant's self-Map reaches
  production readers too. It did with NosFabrica/Brainstorm-UI#306 (merged 2026-10-29 17:21 UTC): production's
  bundle reads a self-Map as "Publishes and keeps up to date" (Out of scope).
- **The repair and Turn off while the GET fails reach the UI with NosFabrica/Brainstorm-UI#307** (open; D6, D4).
  Until it lands, both go through the API alone. The GET now names the relays read by the press that keeps the
  current choice (`writerRelays`), so the UI can ask each one; until #307's probe uses it, a version missed only by
  an outbox relay other than the TA relay waits for the next press, and if it withdrew a claim, that relay serves the
  withdrawn self-entries meanwhile, and the UI offers no press that repairs it.
- **One press at a time is held in-process** (D5).
- **No rate limit on the POST**, like the Duty Menu's.
- **Trusted Lists and Concepts become claimable** once something publishes and keeps them up to date, starting with
  [NosFabrica/brainstorm_server#112](https://github.com/NosFabrica/brainstorm_server/issues/112). `evaluate_live`
  then gains a branch for each. If one is live at a relay the outbox doesn't name, a press that withdraws it there
  sends that relay the new version once, because an earlier version named it: a Turn off, which reads the outbox
  alone, or an Update now that drops it, which reads only the relays of what it still lists. If the relay missed
  that version, no later press reads or sends there again, so it keeps serving the withdrawn self-entries, and
  `writerRelays` doesn't name it (D4, D6). The Update now case was there at `1779d70`; the fourth round added Turn
  off's. A press would then have to read every relay a duty could be live at (every relay the offer names, say), not
  only the relays of what it lists, and `writerRelays` with it.
- **Owed under the Light profile:**
  - this ADR's acceptance;
  - Gate B's ratification of its classification;
  - a review file, Gate B's (`../../workflows/light-profile.md` § Artifacts). The story file was written after the
    build, on 2026-10-28.

## Implementation notes

As built in commit `e2ca872`, with the PR 110 audit's fixes after it:
- `app/services/self_map_build.py` (pure, no I/O):
  - the grammar: `parse_key`, `compare_keys`, `matches`, `covers`, `build_probes`;
  - the caller's Map: `map_groups`, `too_big_to_check` (`MAX_MAP_KEYS`), `duties_of`;
  - the offer and liveness: `Offer`, `scores_upkeep`, `asks_for_scores`, `evaluate_live`;
  - what to list: `build_should_say`, `duty_lines`, `evaluate_self_map`;
  - the writer's side: `self_map_tags`, `read_self_entries`, `nothing_to_change`, `self_map_relays`,
    `self_entry_relays`, `newest`.
- `app/services/self_map_service.py`:
  - the routes' logic: `read_assistant_treasure_map`, `publish_assistant_treasure_map`;
  - the upkeep inputs: `_follow_count` (`cronjobs.scheduler.has_grapevine`, read alongside the caller's Map and
    bounded by `FOLLOWS_TIMEOUT_SECONDS`, never by Turn off), `_scores_upkeep` and `_upkeep_interval`;
  - the relays: `_relays` (the GET's `relays`, with the hints) and `_writer_relays` of `_listed` (a press's, and the
    GET's `writerRelays`, for the current choice);
  - the repair: `_missing`;
  - the choice: `_save_choice`;
  - one press at a time: `_one_press_at_a_time`, over `_pressing`.
- `app/services/assistant_publish.py`, shared with the Duty Menu: `assistant`, `is_own`, `read_newest`,
  `sign_checked`, `send_to`, `reached_some`.
- `app/services/designation_service.py`: `fetch_latest(relay, flt)`, strict, its close bounded at 1 s, and
  `fetch_latest_designation` over it.
- `app/routers/user/router.py`: the two routes.
- `app/schemas/`:
  - `TreasureMapDutyLine`, `AssistantTreasureMapCheck`, `PublishedAssistantTreasureMap`;
  - the shared `AssistantRelayResult`;
  - `SetAssistantTreasureMapBody`;
  - two response wrappers.
- `app/db_models/__init__.py`, `app/repos/brainstorm_nsec.py` (`set_treasure_map_on_by_pubkey_on_db`), and
  `alembic/versions/c5f1d7a9e2b4_add_treasure_map_on.py`.
- Tests: `tests/test_assistant_treasure_map.py` (the cases ported from Tapestry's two self-map suites are named by
  their ids), `tests/test_assistant_publish.py` (`sign_checked`), `tests/test_designated_pubkeys.py`,
  `tests/test_scheduler.py` (`has_grapevine`, and a cycle's no-follows skip) and `tests/test_alembic_heads.py`.

## Out of scope

- Keeping the self-Map true on its own (above).
- Counting or describing Maps (§ 9.2): anything that counts kind-10040 adopters should skip a Map whose public
  entries all name its author, and anything that describes a kind 10040 to people should never describe one as
  entrusting anyone. Brainstorm-UI's `describeDesignation` describes it as what its author publishes ("Publishes and
  keeps up to date"), on UI staging (NosFabrica/Brainstorm-UI#302) and UI production (NosFabrica/Brainstorm-UI#306),
  which it had to reach before any server published a self-Map (Debt).
- Private duties, NIP-85's encrypted `.content`, which never go in a self-Map's public tags.
- Trusted Lists and Concepts as live duties.
- Dictionary management
  ([NosFabrica/brainstorm_server#112](https://github.com/NosFabrica/brainstorm_server/issues/112)).
