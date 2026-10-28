# ADR 0001: Your Assistant's Treasure Map lists what Brainstorm's records show is kept up to date, read against a per-key offer

**Status:** Proposed. Written after the build, on draft PR
[NosFabrica/brainstorm_server#110](https://github.com/NosFabrica/brainstorm_server/pull/110) (commit `e2ca872`). No
Gate A was held. It's for David to accept and for the Gate B reviewer to ratify, together with this ADR's
classification.
**Date:** 2026-10-27
**Story:** none written (see Consequences, "Owed")
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
  Here those are the five `ta_signing.ta_tags` writes.
- **One allowance of two intervals.** One missed run is a brief gap; two in a row is a stop.
- **The writer's rules** (ADR `assistant-self-map/0002` sub-decision 4, Amendment 1 item 2):
  - The writer owns every tag whose element 1 has a kind slot and whose element 2 is the Assistant.
  - The next version keeps every other tag and the content as found, with the new self-entries where the first owned
    tag stood.
  - An `expiration` goes only from beside a self-entry.
  - When the self-entries already match and nothing stale sits beside them, nothing is published.
- **The preservation check** before anything is sent. **One route with a strict boolean body**, where Turn on and
  Update now are both `{on: true}` (ADR `assistant-self-map/0002` P1). **The strict read before a merge** (R1 there).
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
  - the customer's policy, or the default, with its `enabled` and `schedule_interval_seconds`.

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

**Consequence accepted:** a reader whose Map is what `/setup` publishes gets exactly the five Score rows, entry for
entry. A reader who gave a bare `30382` gets the same five, never `30382`.

### D2 — "Live" comes from Brainstorm's own records, not from reading the relay

Scores are **live** when all of these hold:
- `last_time_published_graperank` is set;
- something keeps them up to date: the scheduler's inputs above, or, for the House Observer, the periodic trigger's
  2 hours;
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
  doesn't hold. The schedule is read the way the scheduler admits work, so the check and the scheduler can't
  disagree about whether anything is scheduled.
- **E3: new upkeep stamps written by the publishers.** Not needed: E2's clock already is one.

**Consequences accepted:**
- **The self-Map trusts the server's own record of what landed.** A relay that later loses events isn't noticed
  until the next full run (`full_sync_every_n_runs`) re-asserts them.
- The House Observer special case follows `PERIOD_HOURS`, imported from the cronjob, so the two can't drift.

### D3 — The choice is a column, `brainstorm_nsec.treasure_map_on`

It's a boolean, default false, added by migration `c5f1d7a9e2b4`. Only `POST /user/assistantTreasureMap` writes it,
inside the request's transaction, so a press that answers an error leaves it as it was.

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

### D4 — A press reads strictly, replaces only the self-entries, and checks what it signed

**The reads:**
- The caller's Map is read from our relay, through `designation_service.fetch_latest_designation`:
  - strictly for Turn on: 502 when it can't be read;
  - best-effort for Turn off, which lists nothing and uses the Map only for relay hints.
- Then every relay in the GET's `relays` is asked for the Assistant's kind 10040, the TA relay at its internal URL:
  - only versions validly signed by the Assistant count;
  - the newest by NIP-01 is the base;
  - 502 when no relay answers.
- The designation reader now raises on `CLOSED`, so a refusal never reads as "none".
  - nostr-sdk can't tell a silent relay from an empty one; both give zero events and no error.
  - So these reads use the reader's plain REQ-until-EOSE.

**Options considered:**
- **R1 (chosen): strict, as Tapestry's R1.** A kind 10040 can hold entries other than self-entries, so a press must
  not build over nothing.
- **R2: best-effort, as the Duty Menu reads** (ADR `assistant-duty-menu/0001` D5). **Rejected** for that reason. A
  menu holds nothing else; a Map can.

**How this differs from Tapestry's R1:**
- **No "this instance's relay must answer".** Brainstorm has no local relay.
- **No stored copy of the last version** (Tapestry ADR `assistant-self-map/0002` Amendment 3). If every relay that
  holds the newest version is silent while another answers empty, the press builds over less. Today nothing else
  signs the Assistant's kind 10040, so its tags are the self-entries and the `client` tag, and nothing is lost.
  Revisit if anything else ever writes it.

**The merge:**
- It follows Tapestry's rules above.
- The earlier version's `client` tag is replaced by ours, not kept beside it.
- The new version is dated `max(now, newest version found + 1)` (§ 9.2 "When it changes").

**The check before sending:**
- the signed event verifies;
- its tags are exactly what was built;
- it is the next version of what was found: the same content, the target as its self-entries, and every other tag
  kept.

If any of that fails, the press is a 500.
- `allow_self_tagging()` keeps a `p` carried over from the version found.
- Self-entries themselves name the signer in element 2, which nostr-sdk keeps without it.

**Where it goes:**
- the GET's `relays`: the Assistant's outbox, every relay the self-entries name, and the caller's hints for the
  Assistant;
- every relay a self-entry of a version found named (§ 9.2 "Finding a self-Map").

It goes through the Duty Menu's relay I/O, now shared (`connect_targets`, `send_event`, `relay_report`). Tapestry
left that sharing for later (its S2) because its Duty Menu route had shipped. Neither route had shipped here.

### D5 — One press at a time per Assistant, held in this process

**Options considered:**
- **A (chosen): an `asyncio.Lock` per Assistant** in `self_map_service`. The server runs one replica with one worker
  (see the repo facts), so it holds today.
- **B: a Redis lock**, as `leader_lock`. **Deferred:** it's needed only if the server scales out, and the module's
  comment says so.
- **C: no lock.** **Rejected:** two presses at once read the same version and sign two with the same `created_at`.
  NIP-01 keeps the lower id, so a Turn off racing a Turn on would leave either one (§ 9.2 "When it changes"). A test
  runs two at once.

### D6 — The server answers what to list; the UI reads the published version itself

The GET answers `{assistant, on, duties, shouldSay, relays}`. Its only relay read is our own relay, for the caller's
Map. The UI reads the Assistant's kind 10040 from `relays` and compares it with `shouldSay`, as a set of
`(key, relay)`, as it does for the Duty Menu. Its Nostr reads come from its EventStore (Brainstorm-UI ADR 0005).

**Options considered:**
- **A (chosen):** as above. The answer carries no `published`, `current`, `falseClaim` or `active`; the UI works them
  out. `duties` lines are `live`, `offered` (with a `reason`), `not-offered` or `elsewhere`. `entries` lists the
  `shouldSay` entries that list a live line.
- **B: the found version rides the answer**, as Tapestry's ADR `assistant-self-map/0003`. **Rejected:**
  - every page load would cost an outside relay round;
  - it would add a second, server-side copy of a Nostr read the UI already holds and keeps current.

**The cost:** the UI reads a version as the press does, so the two must agree on the writer's rules. That is a
cross-repo contract. NosFabrica/Brainstorm-UI#301 aligned the UI with `nothing_to_change`: the kind slots the writer
owns, each key and relay once, and the `expiration` and stray-tag rule.

### D7 — The wire: camelCase, plain sentences, nothing changed on an error

- **camelCase** (`shouldSay`, `lastLandedAt`), as the UI's parser reads it, through Pydantic serialization aliases.
  This departs from the repo's snake_case for new endpoints (`app/schemas/CLAUDE.md`), because the UI's contract was
  written first and is on staging.
- **Wrapped in `{data}`**, like every `/user` answer.
- **On the authenticated `/user` router**, as the Duty Menu's (its D7).
- **The body** is `{on: StrictBool}`; anything else is a 422.
- **Errors are plain sentences** in `detail` (`app/services/CLAUDE.md` § Errors):
  - 502 when the caller's Map can't be read (the GET, and Turn on), when no relay answers the read, or when no relay
    takes the new version;
  - 500 when the signed event fails its own check.
- **`reason` uses only the words the UI has:** `not-landed`, `not-scheduled`, `upkeep-stopped`,
  `not-published-here`, `nothing-to-publish`, `unchecked`.
- **Nothing to change** answers `{on, published: false, event: null, relays: []}`.

## Consequences

**Enables**
- The UI's fifth setup step, with Tapestry's buttons.
- A client that sees the self-entries can take the customer's Scores from this Assistant at once, and count them as
  maintained at the Assistant's relay (§ 9.2 "Confirmed duties", "Maintained").
- The Duty Menu and the Treasure Map share one offer and one relay I/O.

**Constrains / makes harder**
- **The self-Map is only as true as the publish clock and the scheduling policies.** A change to how either works
  changes what it claims.
- **Scores are listed as five entries, not one** (D1), and the published event is the self-entries plus `client`.
- **The writer's rules are now a contract with Brainstorm-UI** (D6), and with Tapestry's readers through § 9.2.

**Debt and follow-ups**
- **It doesn't keep itself true.** § 9.2 says a key MUST remove or narrow a self-entry before it stops keeping it up
  to date. Until something republishes when the answer changes, the UI shows the Map out of date and the customer
  presses Update now. Natural hooks are a successful TA publish, a policy disabled, or an entitlement lapsing. This is
  Tapestry's story 5, not built there either.
- **A relay that refused a removal keeps serving the old claim**, and a second Turn off then says nothing changed.
  Same as Tapestry's ledger row `2026-10-13-self-map-refused-removal-stays-outside`.
- **The lock is in-process** (D5).
- **No rate limit on the POST**, like the Duty Menu's.
- **Trusted Lists and Concepts become claimable** once something publishes and keeps them up to date, starting with
  [NosFabrica/brainstorm_server#112](https://github.com/NosFabrica/brainstorm_server/issues/112). `evaluate_live`
  then gains a branch for each.
- **Owed under the Light profile:**
  - this ADR's acceptance;
  - Gate B's ratification of its classification;
  - a story file and a review file (`../../workflows/light-profile.md` § Artifacts). None was written while building.

## Implementation notes

As built in commit `e2ca872`:
- `app/services/self_map_build.py` (pure, no I/O):
  - the grammar: `parse_key`, `compare_keys`, `matches`, `covers`, `build_probes`;
  - the caller's Map: `map_groups`, `duties_of`;
  - the offer and liveness: `Offer`, `scores_upkeep`, `evaluate_live`;
  - what to list: `build_should_say`, `duty_lines`, `evaluate_self_map`;
  - the writer's side: `owns_tag`, `kept_tags`, `build_self_map_tags`, `nothing_to_change`, `is_next_version`,
    `self_map_relays`, `self_entry_relays`, `newest`.
- `app/services/self_map_service.py`:
  - the routes' logic: `read_assistant_treasure_map`, `publish_assistant_treasure_map`;
  - the upkeep inputs: `_upkeep_interval`;
  - the strict read: `_read_versions`;
  - signing: `_sign`;
  - the lock: `_one_press_at_a_time`.
- `app/services/designation_service.py`: `fetch_latest_designation` is public and raises on `CLOSED`.
- `app/services/duty_menu_service.py`: `connect_targets`, `send_event`, `relay_report` are public.
- `app/routers/user/router.py`: the two routes.
- `app/schemas/`:
  - `TreasureMapDutyLine`, `AssistantTreasureMapCheck`, `PublishedAssistantTreasureMap`;
  - the shared `AssistantRelayResult`;
  - `SetAssistantTreasureMapBody`;
  - two response wrappers.
- `app/db_models/__init__.py`, `app/repos/brainstorm_nsec.py` (`set_treasure_map_on_by_pubkey_on_db`), and
  `alembic/versions/c5f1d7a9e2b4_add_treasure_map_on.py`.
- Tests: `tests/test_assistant_treasure_map.py` (the cases ported from Tapestry's two self-map suites are named by
  their ids) and `tests/test_designated_pubkeys.py`.

## Out of scope

- Keeping the self-Map true on its own (above).
- Counting Maps (§ 9.2): anything that counts kind-10040 adopters should skip a Map whose public entries all name
  its author.
- Private duties, NIP-85's encrypted `.content`, which never go in a self-Map's public tags.
- Trusted Lists and Concepts as live duties.
- Dictionary management
  ([NosFabrica/brainstorm_server#112](https://github.com/NosFabrica/brainstorm_server/issues/112)).
