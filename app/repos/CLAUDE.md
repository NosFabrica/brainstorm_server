# app/repos

Data-access layer. Every DB read/write goes through here — services and routers
never touch SQLAlchemy `Select`s or Cypher strings directly.

## Files

| File | Backing store | Purpose |
|---|---|---|
| `brainstorm_request_repo.py` | PostgreSQL (`brainstorm_request`) | GrapeRank job lifecycle: CRUD, status transitions, stale sweep, recent-activity statement builders |
| `brainstorm_nsec.py` | PostgreSQL (`brainstorm_nsec`) | Per-user observer keypair + preset params + last-published-pubkeys binary blob |
| `session_version_repo.py` | Redis over PostgreSQL (`brainstorm_nsec.session_version`) | Read-through cache of a user's session version (60s TTL, the revocation bound) + `cache_session_version` for write-through on a bump. The one read path for both `verify_token` and login |
| `graperank_preset_repo.py` | PostgreSQL (`graperank_preset`, `graperank_preset_history`) | Builtin preset CRUD + audit-log helpers + camelCase ↔ snake_case converters |
| `brainstorm_nostr_transferer.py` | PostgreSQL (`brainstorm_nostr_relay_transfer`) | Relay-sync state machine (per-kind cursor + completion) |
| `short_url_repo.py` | PostgreSQL (`short_url`) | Share-link records: resolve by code, dedup lookup by (pubkey, relay-set fingerprint), insert |
| `passkey_vault_repo.py` | PostgreSQL (`passkey_vault`) | Passkey vault rows: by credential hash, by owner, an owner-only `ON CONFLICT DO UPDATE` upsert (returns nothing for someone else's hash; resets the counter), a conditional mark-used that only moves the counter forward, owner-scoped delete |
| `tagging_repo.py` / `event_tagging_repo.py` | PostgreSQL (`nostr_tag_element`, `nostr_user_tagging` / `nostr_tagging_header`, `nostr_event_tagging`, `nostr_tagged_event`) | Trusted Lists input: tag elements and pubkey taggings; tagging headers, event taggings and the by-id lookup cache. See the sections below |
| `compromised_pubkey_repo.py` | PostgreSQL (`compromised_pubkey`) | Accounts marked compromised: the set each pipeline stage reads once per run, list, upsert, delete |
| `user_id_repo.py` | PostgreSQL, **Scorecard database** (`nostr_user`) | Pubkey ↔ User id: `ids_for(pubkeys, create=True)` and `pubkeys_for(user_ids)`. The only code that writes `nostr_user`. Opens and commits its own session (no `db` argument), so an id is durable before the caller writes it to Neo4j. A known pubkey never draws from the sequence. Also `count_users`, `all_user_ids` and `restore_users` (explicit ids, after a restore only) for `scripts/backfill_user_ids.py` |
| `scorecard_repo.py` | PostgreSQL, **Scorecard database** (`scorecard`) | One row per (Observer, Observee): `influence`, `hops`, `verified_followers`. Callers pass pubkeys and the caller's session; the statements translate through `nostr_user` (rows are `COPY`'d into a temp table and joined). Set (upsert, unchanged rows skipped), clear by pubkey, clear everyone not reached, list, and clear one Observee for every Observer (a table scan, used when an account is marked compromised). A scored pubkey with no user id raises `MissingUserIds`. `scorecard_observer` holds when each Observer's Scorecards last changed (`mark_observer_written_on_db`, called in the transaction that changes them; `observer_written_at`): the whitelist endpoint's version stamp |
| `user_repo.py` | **Neo4j** | All Cypher queries for the social graph (follows/mutes/reports + influence-weighted counts/paginations) |
| `billing_plan_repo.py` | PostgreSQL (`billing_plan`) | Mapping CRUD — Flash plan → scheduling policy, and whether we sell it. Listed by `id`, which is a stable order rather than a meaningful one: display order is Flash's `sortOrder`. `is_active` means *sellable*: only the plans-for-sale listing filters on it, never the entitlement lookup. There is **no** scheduling-id → plan lookup — a subscriber's tier is their policy, read straight off the assignment |
| `support_repo.py` | PostgreSQL (`support_ticket`, `support_message`, `support_event`) | Ticket list statement builders (newest activity first), the unclosed-ticket count behind the cap and the per-pubkey advisory lock that makes it hold, plain inserts, the thread reads — messages and events ordered by **`id`**, not `created_at`, which ties when two rows land in the same millisecond — `lock_support_ticket_on_db` (the `FOR UPDATE` read every mutation opens with), and `build_admin_support_tickets_stmt` — the queue, with each filter applied only when given, so an unfiltered queue constrains nothing. `build_expired_diagnostics_stmt` / `clear_expired_support_diagnostics_on_db` clear the snapshot past its retention window — dated from filing, skipping rows already cleared. `select_user_support_digest_on_db` / `select_admin_support_digest_on_db` back the conditional GET: `max(updated_at), count(*)` over the same rows the listing would return, through the shared `_admin_support_filters` so the two can never disagree about scope. **`updated_at`, never `last_message_at`** — resolving, recategorizing and a message-less reopen change what the client renders while touching no message, and a validator keyed on message activity reports those as unchanged forever. **Unindexed on purpose (for now):** nothing indexes `updated_at`, `status` or `category`, so the admin digest is a scan. It is still strictly cheaper than the listing it guards — same rows, no sort, no limit, no serialization — which is the bar. If polling ever shows up in slow queries, `(pubkey, updated_at desc)` and `(updated_at desc)` are the indexes to reach for; pick them off a real plan rather than a guess. No ticket-field writes beyond insert — the service's private helpers own those |
| `scheduling_repo.py` | PostgreSQL (`scheduling`) | Policy CRUD, the default policy, and the public-policies selector that gates `/billing/plans` |
| `user_subscription_repo.py` | PostgreSQL (`user_subscription`) | Subscription upsert, subscriber lock, reconcile/lapse candidates, divergence reads (policy mismatches, stale/failing syncs, unrecognised statuses, retired-plan subscribers), and the per-plan subscriber count that decides whether a mapping's Flash ids may still be rewritten |
| `flash_webhook_event_repo.py` | PostgreSQL (`flash_webhook_event`) | The inbox: insert/claim/replay, unresolved signups vs unmapped plans (two failures, selected apart), exhausted events, settling one by hand |

## Conventions (read these once, save yourself debugging)

### Async-first

Every public function is `async def` and takes the session/driver as the first
positional arg: SQL repos take `AsyncSession`, `user_repo.py` takes
`AsyncNeoSession` (a Neo4j *session*, aliased; it was long mis-aliased as `AsyncNeoDriver` until the 2026-08-26 gate-greening). Don't sneak in sync calls.

### Transactions live in the caller

SQL repo functions do NOT commit. They `add()`, `update()`, `flush()`, or call
`db.execute()`. The caller wraps with `async with db_session() as db:` from
`app.core.database`, and the context manager handles commit on success /
rollback on exception.

**One exception**: `brainstorm_nostr_transferer.upsert_nostr_transfer_status_on_db`
calls `await db.commit()` itself. That's inconsistent with the rest — be aware
when reading the code; consider normalizing if you touch it.

### Naming

Functions end with `_on_db` to signal the side effect channel:
- `select_brainstorm_request_by_id_on_db`
- `update_brainstorm_request_status_by_id_on_db`
- `delete_brainstorm_request_by_id_on_db`
- `create_brainstorm_request_on_db`

Statement *builders* (return a `Select` to be paginated/executed by the caller)
use `build_*_stmt`:

- `build_recent_brainstorm_requests_stmt`
- `build_recent_active_pubkeys_stmt`

### Deferred / heavy columns

`select_brainstorm_nsec_history_fields_on_db` defers the binary
`last_published_pubkeys` column. Reach for `.options(defer(...))` whenever a
row's large enough to matter. (The old ~100MB `brainstorm_request.result` blob
was removed — the per-observer whitelist now lives in `observer_whitelist_repo`.)

### Encrypted nsec

`select_brainstorm_nsec_by_pubkey_on_db` decrypts the nsec on read via
`_resolve_plaintext_nsec`. Plaintext fallback exists for unmigrated rows. Don't
read `BrainstormNsec.nsec` directly outside the repo — go through the helper.
The Assistant pubkey (public half of the nsec) is derived by `_assistant_pubkey`
once, when the row is created, and stored with its NIP-05 local-part
(`assistant_pubkey`, `nip05_local_part`); rows that predate the columns are
filled at startup by `backfill_assistant_identities_on_db`.

### Binary-packed pubkey lists

`BrainstormNsec.last_published_pubkeys` is a `LargeBinary` with no separator —
back-to-back 32-byte hex pubkey strings. `_unpack_pubkeys` / `_pack_pubkeys`
in `brainstorm_nsec.py` are the only safe entry points.

## Public surface highlights

### `brainstorm_request_repo.py`

- `create_brainstorm_request_on_db(db, algorithm, parameters, pubkey, graperank_preset_used, graperank_params) → BrainstormRequest`
- `select_latest_brainstorm_request_on_db(db, pubkey) → BrainstormRequest | None` (+ `non_waiting`, `successful` variants)
- Status mutators: `update_brainstorm_request_status_by_id_on_db`, `…_ta_status_by_id_on_db`, `…_internal_publication_status_by_id_on_db`, `update_brainstorm_request_result_by_id_on_db`
- `fail_stale_in_pipeline_brainstorm_requests_on_db(db, stale_threshold) → list[int]` (reaped run ids; calc or TA publish non-terminal past the cutoff; used by the cronjob in `app/cronjobs/`). `update_brainstorm_request_result_by_id_on_db` only writes over a non-terminal calc status, so a reaped run can't be resurrected.
- `compute_admin_stats_on_db(db) → dict` (queue_depth, scored_users, sp_adopters — single round-trip)

### `brainstorm_nsec.py`

- `get_or_create_brainstorm_observer_nsec_by_pubkey_on_db(db, pubkey) → tuple[BrainstormNsec, bool]` (the bool is "created now")
- `select_assistant_pubkey_by_nip05_local_part_on_db(db, local_part) → str | None` — indexed lookup behind the NIP-05 well-known route (`pubkey` is the owner, `assistant_pubkey` the Assistant).
- Preset getters/setters: `get_graperank_preset_by_pubkey_on_db`, `set_graperank_preset_by_pubkey_on_db`, `get_graperank_custom_params_by_pubkey_on_db`, `set_graperank_custom_params_by_pubkey_on_db` (all auto-create the row if absent)
- `set_treasure_map_on_by_pubkey_on_db(db, pubkey, on)` — the Assistant's Treasure Map choice; written only by `POST /user/assistantTreasureMap`
- Timestamp updates: `update_last_time_triggered_graperank_on_db`, `update_last_time_calculated_graperank_on_db`
- Published-pubkeys list: `get_last_published_pubkeys_by_pubkey_on_db`, `update_last_published_pubkeys_by_pubkey_on_db`

### `graperank_preset_repo.py`

- `get_all_presets_on_db(db) → list[GrapeRankPreset]`
- `get_preset_on_db(db, preset_id) → GrapeRankPreset | None`
- `update_preset_on_db(db, preset_id, params_camel, changed_by) → GrapeRankPreset` (mutates + appends history row in one go)
- `get_preset_history_on_db(db, preset_id, limit=100) → list[GrapeRankPresetHistory]`
- `row_to_camel_dict` / `camel_dict_to_columns` — the snake↔camel bridge. Use them; never inline the mapping.

### `brainstorm_nostr_transferer.py`

- `get_nostr_transfer_status_by_kind_from_db(db, kind) → row | None`
- `upsert_nostr_transfer_status_on_db(db, kind, completed, total_events, oldest, started_at) → None` (commits internally — the inconsistency mentioned above)

### `user_repo.py` (Neo4j)

30 async functions, raw Cypher. Patterned around three relations (`FOLLOWS`,
`MUTES`, `REPORTS`) × two directions (in/out). For each (rel, direction) there
are `get_list_of_pubkeys_*` and `count_*` helpers. Plus the bigger composite
queries:

- `get_trust_signals_for_pubkeys(session, pubkeys, influence_key, *, verified_line, reporter_cutoff) → {pubkey: TrustSignalRow(influence, verified, flagged)}` — `/overview`'s verdicts for many subjects in one UNWIND, expanding the same `_VERIFIED_LINE` / flagged predicates onto `user`.
- `get_outbound_counts_and_influence(session, pubkey, influence_key, *, verified_line, reporter_cutoff) → OutboundOverview(influence, following, muting, reporting, flagged_by_observer, flagged_count, verified, tier)` — single round-trip vs four sequential. `verified`/`tier` are the subject's *own* verdict, off the same `_TIER_PREDICATES` table the section rows use.
- `get_paginated_section_connections(session, pubkey, influence_key, rel_type, direction, limit, cursor_inf, cursor_pk, …)` → `(items, next_cursor, total)` — cursor = `(influence, pubkey)`; ordered influence DESC, pubkey ASC. **Drives `/user/{pubkey}/connections`**. `verified_cutoff` is that section's preset cutoff — every row reports whether it clears it (`verified`) and which bucket it lands in (`tier`), and `verified_only` narrows the page to the verified rows. `verified_line` is the tier fallthrough boundary. There is no client-supplied `min_influence`.
- `get_all_section_stats(...)` — one query covering all 6 sections; ~20 % faster than firing them in parallel.
- `set_observer_scores(session, rows)` / `clear_observer_scores(session, pubkeys, keys)` / `get_pubkeys_scored_by_observer(session, keys)` — the GrapeRank result writer's side (`write_neo4j_results.py`). The two writes run in a managed transaction (retried on transient errors); the read is a `:NostrUser` label scan, so the writer runs it only for cleanup.
- `get_user_graph_data(session, pubkey, influence_key, *, reporter_cutoff)` — unpaginated full graph; row `trusted_reporters` is the live `_live_reporters` count.
- `get_network_alert_candidates(...)` / `count_above_cutoff_followers_capped(...)` / `count_verified_muters(...)` — the three bounded steps behind **`/networkAlerts`** (orchestrated by `network_alerts_service`). Things to preserve if you touch them:
  - Candidates are anchored *through* the `REPORTS` edge, never a `:NostrUser` label scan. No index can substitute — the influence properties are per-observer, so indexing them would mean one index per observer.
  - `verified_reporter_count` is live: `REPORTS` edges from raters above the observer's preset reporter cutoff (strict `>`). `>= MIN_ALERT_REPORTERS` (5) prefilters before any arithmetic. Valid because `N = 4 + floor(tf/500) >= 4` for everyone. `BASE_REPORTER_THRESHOLD` (4) and `FOLLOWERS_PER_EXTRA_REPORT` (500) both live in `user_repo.py` so the prefilter and the service's arithmetic can't drift; raise the base without the prefilter and the cap below turns into a zero-or-negative Cypher LIMIT.
  - The capped count's `cap` is **not** an arbitrary page size. A row alerts only when its verified follower count is below `500 * (reporters - 4)`, so a count reaching the cap belongs to a row that fails. That's why a count *below* the cap is exact and safe to publish, and why the service drops rows that hit it.
  - Step 2 runs only for candidates with no stored `trusted_followers_<observer>`, and goes quiet as observers backfill. `write_neo4j_results.py` persists that property, making it a **third** per-observer property — note the cost: Neo4j walks a linked list of property records to resolve a key, and every distinct property *name* permanently consumes a global property-key token that is never reclaimed. Both scale with observer count.

**Per-observer node properties** written by `write_neo4j_results.py`, all suffixed
with the observer's hex pubkey: `influence_`, `hops_`, `trusted_followers_`.
Reporter counts are not stored; reads count `REPORTS` edges live. All three are GrapeRank-run-fresh, NOT ingest-fresh — the
`FOLLOWS`/`MUTES`/`REPORTS` edges update within seconds of a kind 3/10000/1984
event, but these properties only change when that observer's GrapeRank job
lands. Prefer an edge match over `hops_<observer> = 1` when you need "does X
currently follow Y".

**One tier table, three expanders.** `_TIER_PREDICATES` is expanded by
`get_all_section_stats` (the `/stats` bucket counts), by `_build_tier_predicate`
(the `/connections?tier=…` filter), and by `_tier_case` (the per-row `tier` on
`/connections` items and the subject's own `tier` on `/overview`), so none of
them can disagree about which subject sits in which bucket. The flagged bucket
counts verified reporters live off `REPORTS` edges (`_live_reporters`: reporter
influence strictly above the preset's `reporter_cutoff`), never a stored count. The same fallthrough
is also written in Python (`tier_thresholds.classify_tier`) for the GrapeRank
result writer, which buckets scorecards before they reach the graph and so has
no query to run; `tests/integration/test_tier_classifier_matches_cypher.py`
asserts the two agree. The band bounds come
from `_tier_band_params()` — the `TIER_*` constants, bound the same way by every
expander. No endpoint takes band overrides: a client that could move a band could
make `/connections?tier=…` return rows `/stats` counted in a different bucket.
Verified is strict `>` the
verified line, so `_VERIFIED_LINE` / `_UNVERIFIED_LINE` are exact complements;
`_UNVERIFIED_LINE` includes "no stored influence" (out of reach, or at or below
the worker's persist floor), so a reported near-zero account is still flagged.
Let them drift and a subject sitting exactly on the line lands in no
bucket. `get_outbound_counts_and_influence` and
`get_paginated_flagged_connections` share the same reading of "flagged", which
is why `/overview`'s `flagged_count` matches `/connections?kind=flagged`.

Dynamic property access uses `user[$influence_key]` parameterization so
`influence_<observer>` columns don't get string-interpolated into Cypher.
Don't write the property name into the query string — a per-observer name in the
query text gives every observer its own Neo4j plan-cache entry. The write side
follows the same rule from the other direction: `write_neo4j_results.py` builds
the `{name: value}` map in Python and `set_observer_scores` does `SET n += row.props`.

**Score reads from the Scorecard database (`SCORES_IN_POSTGRES`, default off).** Reads are
graph first: Cypher answers "who" by `user_id` and never touches a score property, then
`scorecard_repo.scores_by_id` / `scores_by_pubkey` (the one read helper, opening its own session)
give pubkeys and scores, and the verdicts (`verified`, `tier`, flagged, live reporter counts) are
computed in Python with `tier_thresholds.classify_tier`. Each converted function keeps its Neo4j
form behind the setting; the Scorecard form lives beside it as `_…_from_scorecards`. A graph id
missing from `nostr_user` raises rather than returning a partial answer.
The paged and counted reads (`/connections`, flagged, `/stats`, top inbound) take ids only from the
graph, in one collected list per section plus the reporters of the few members anyone reports, and
tier, filter, sort and page in SQL (`scorecard_repo.page_of_members` / `count_members`, whose
`_TIER_SQL` restates `classify_tier`). The unpaginated graph data reads every member's row.
Live counts (alert reporters, verified muters, the capped follower fallback) are two steps: the
graph returns each subject's raters as maps (`_raters`, `{id}` today, room for a report's reason),
`scorecard_repo.ids_above` says which sit above the Observer's cutoff. Observer-wide reads
(`get_ta_observees_for_observer`) are one indexed range over the Observer's rows, no graph read.
`tests/test_score_reads_go_through_scorecards.py` fails on a new score read that is not switched
(its not-converted list is empty);
`tests/integration/test_score_reads_equivalence_integration.py` holds both forms to the same answer.

**The whitelist** (an Observer's Valid users) is a stored JSON snapshot in `observerwhitelist`
(`observer_whitelist_repo.py`) until `SCORES_IN_POSTGRES`; with it on, nothing writes the snapshot
and `user_service.get_whitelisted_pubkeys_of_observer` derives the list from Scorecards: Influence
rounded to two decimals in Python (as the snapshot was), at or above the validity cutoff, in pubkey
order. A changed cutoff therefore applies at read time, not at the Observer's next run.

## Common tasks

| I want to… | Do this |
|---|---|
| Add a new SQL table | Add the SQLAlchemy model in `app/db_models/`, run `alembic revision --autogenerate -m "…"`, then a new repo module here |
| Add a query over an existing SQL table | New function in the matching `*_repo.py`. Pattern: `async def <verb>_<noun>_<by_what>_on_db(db, ...) → …` |
| Add a new Cypher query | Append to `user_repo.py`. Parametrize with `$pubkey` etc., never f-string the values. Dynamic prop names go via `user[$key]`. |
| Paginate a new list | SQL → `build_*_stmt` returning a `Select`, the router calls `paginate(db, stmt, …)`. Graph → cursor tuple pattern from `get_paginated_section_connections`. |
| Audit changes to a config table | Mirror the `graperank_preset_history` pattern (history table + repo append + `change_type` + `changed_by`) |

## Gotchas

- **No commits in repos** (except the one noted above). If your DB writes mysteriously don't persist, you forgot `async with db_session() as db:` around the call.
- **Read a node's Influence once per row.** Every `node[$influence_key]` walks the node's whole property list, and a scored node carries thousands of properties. Bind it with `_bind_score` and use `_score(node)` afterwards; `tests/test_score_read_once.py` holds the tier fragments to that.
- **Never alias a Cypher value as `inf`, `infinity` or `nan`.** They are float literals: `WITH x AS inf RETURN inf` returns Infinity on every row.
- **`InvalidParameterError` on Cypher with dynamic property keys** means you string-interpolated when you should've parametrized — use `user[$key]`.
- **`fastapi_pagination.paginate` requires a `Select`**, not a coroutine. Build the statement in the repo, hand the statement (not the result) to the router.

## `tagging_repo.py`

kind-39999 tag elements and taggings. **Replaceability is enforced at write
time**, not by a read-time dedupe: both upserts are latest-wins on the event's
own `created_at` (never ingest time), so an out-of-order delivery can't regress
a row and an apply→dispute flip replaces the prior stance rather than
double-counting. Reads are therefore plain aggregates.

`get_dictionary_on_db` joins through to the element table, so a tagging
referencing an element we never ingested is dropped rather than producing a
Trusted List with an empty title.

## `event_tagging_repo.py`

Tagging headers, event taggings and the by-id lookup cache (ADR
`trusted-lists/0002` D6). Same write-time replaceability as `tagging_repo`:
headers latest-wins at their coordinate, event taggings at (asserter, d-tag).
`get_event_taggings_on_db` joins each tagging through its header (by the
descriptor coordinate) to its Tag element (by coordinate or by event id), so a
tagging whose header or Tag hasn't arrived is dropped at read time. Neutral
polarities are returned: a newest neutral stance still replaces an older one.
`nostr_tagged_event` holds only lookups **by event id** (immutable, so cached
forever, and insert-only); nothing named by coordinate is stored.
