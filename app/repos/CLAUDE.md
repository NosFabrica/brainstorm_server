# app/repos

Data-access layer. Every DB read/write goes through here — services and routers
never touch SQLAlchemy `Select`s or Cypher strings directly.

## Files

| File | Backing store | Purpose |
|---|---|---|
| `brainstorm_request_repo.py` | PostgreSQL (`brainstorm_request`) | GrapeRank job lifecycle: CRUD, status transitions, stale sweep, recent-activity statement builders |
| `brainstorm_nsec.py` | PostgreSQL (`brainstorm_nsec`) | Per-user observer keypair + preset params + last-published-pubkeys binary blob |
| `graperank_preset_repo.py` | PostgreSQL (`graperank_preset`, `graperank_preset_history`) | Builtin preset CRUD + audit-log helpers + camelCase ↔ snake_case converters |
| `brainstorm_nostr_transferer.py` | PostgreSQL (`brainstorm_nostr_relay_transfer`) | Relay-sync state machine (per-kind cursor + completion) |
| `user_repo.py` | **Neo4j** | All Cypher queries for the social graph (follows/mutes/reports + influence-weighted counts/paginations) |
| `billing_plan_repo.py` | PostgreSQL (`billing_plan`) | Mapping CRUD — Flash plan → scheduling policy, and whether we sell it. Listed by `id`, which is a stable order rather than a meaningful one: display order is Flash's `sortOrder`. `is_active` means *sellable*: only the plans-for-sale listing filters on it, never the entitlement lookup. There is **no** scheduling-id → plan lookup — a subscriber's tier is their policy, read straight off the assignment |
| `scheduling_repo.py` | PostgreSQL (`scheduling`) | Policy CRUD, the default policy, and the public-policies selector that gates `/billing/plans` |
| `user_subscription_repo.py` | PostgreSQL (`user_subscription`) | Subscription upsert, subscriber lock, reconcile/lapse candidates, divergence reads (policy mismatches, stale/failing syncs, unrecognised statuses, retired-plan subscribers), and the per-plan subscriber count that decides whether a mapping's Flash ids may still be rewritten |
| `flash_webhook_event_repo.py` | PostgreSQL (`flash_webhook_event`) | The inbox: insert/claim/replay, unresolved signups vs unmapped plans (two failures, selected apart), exhausted events, settling one by hand |

## Conventions (read these once, save yourself debugging)

### Async-first

Every public function is `async def` and takes the session/driver as the first
positional arg: SQL repos take `AsyncSession`, `user_repo.py` takes
`AsyncNeoDriver`. Don't sneak in sync calls.

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

### Binary-packed pubkey lists

`BrainstormNsec.last_published_pubkeys` is a `LargeBinary` with no separator —
back-to-back 32-byte hex pubkey strings. `_unpack_pubkeys` / `_pack_pubkeys`
in `brainstorm_nsec.py` are the only safe entry points.

## Public surface highlights

### `brainstorm_request_repo.py`

- `create_brainstorm_request_on_db(db, algorithm, parameters, pubkey, graperank_preset_used, graperank_params) → BrainstormRequest`
- `select_latest_brainstorm_request_on_db(db, pubkey) → BrainstormRequest | None` (+ `non_waiting`, `successful` variants)
- Status mutators: `update_brainstorm_request_status_by_id_on_db`, `…_ta_status_by_id_on_db`, `…_internal_publication_status_by_id_on_db`, `update_brainstorm_request_result_by_id_on_db`
- `fail_stale_ongoing_brainstorm_requests_on_db(db, stale_threshold) → int` (rowcount; used by the cronjob in `app/cronjobs/`)
- `compute_admin_stats_on_db(db) → dict` (queue_depth, scored_users, sp_adopters — single round-trip)

### `brainstorm_nsec.py`

- `get_or_create_brainstorm_observer_nsec_by_pubkey_on_db(db, pubkey) → tuple[BrainstormNsec, bool]` (the bool is "created now")
- `select_all_assistant_pubkeys_on_db(db) → list[str]` — every Assistant pubkey, derived from the rows' nsecs (the Assistant key is not a column; `pubkey` is the owner). Drives the NIP-05 well-known lookup.
- Preset getters/setters: `get_graperank_preset_by_pubkey_on_db`, `set_graperank_preset_by_pubkey_on_db`, `get_graperank_custom_params_by_pubkey_on_db`, `set_graperank_custom_params_by_pubkey_on_db` (all auto-create the row if absent)
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

27 async functions, raw Cypher. Patterned around three relations (`FOLLOWS`,
`MUTES`, `REPORTS`) × two directions (in/out). For each (rel, direction) there
are `get_list_of_pubkeys_*` and `count_*` helpers. Plus the bigger composite
queries:

- `get_outbound_counts_and_influence(session, pubkey, influence_key, trusted_reporters_key, verified_line, …) → OutboundOverview(influence, following, muting, reporting, flagged_by_observer, flagged_count, verified, tier)` — single round-trip vs four sequential. `verified`/`tier` are the subject's *own* verdict, off the same `_TIER_PREDICATES` table the section rows use.
- `get_paginated_section_connections(session, pubkey, influence_key, rel_type, direction, limit, cursor_inf, cursor_pk, …)` → `(items, next_cursor, total)` — cursor = `(influence, pubkey)`; ordered influence DESC, pubkey ASC. **Drives `/user/{pubkey}/connections`**. `verified_cutoff` is that section's preset cutoff — every row reports whether it clears it (`verified`) and which bucket it lands in (`tier`), and `verified_only` narrows the page to the verified rows. `verified_line` is the tier fallthrough boundary. There is no client-supplied `min_influence`.
- `get_all_section_stats(...)` — one query covering all 6 sections; ~20 % faster than firing them in parallel.
- `get_user_graph_data(...)` — unpaginated full graph.
- `get_network_alert_candidates(...)` / `count_above_cutoff_followers_capped(...)` / `count_verified_muters(...)` — the three bounded steps behind **`/networkAlerts`** (orchestrated by `network_alerts_service`). Things to preserve if you touch them:
  - Candidates are anchored *through* the `REPORTS` edge, never a `:NostrUser` label scan. No index can substitute — the influence/reporter properties are per-observer, so indexing them would mean one index per observer.
  - `verified_reporter_count >= MIN_ALERT_REPORTERS` (5) prefilters before any arithmetic. Valid because `N = 4 + floor(tf/500) >= 4` for everyone. `BASE_REPORTER_THRESHOLD` (4) and `FOLLOWERS_PER_EXTRA_REPORT` (500) both live in `user_repo.py` so the prefilter and the service's arithmetic can't drift; raise the base without the prefilter and the cap below turns into a zero-or-negative Cypher LIMIT.
  - The capped count's `cap` is **not** an arbitrary page size. A row alerts only when its verified follower count is below `500 * (reporters - 4)`, so a count reaching the cap belongs to a row that fails. That's why a count *below* the cap is exact and safe to publish, and why the service drops rows that hit it.
  - Step 2 runs only for candidates with no stored `trusted_followers_<observer>`, and goes quiet as observers backfill. `write_neo4j_results.py` persists that property, making it a **fourth** per-observer property — note the cost: Neo4j walks a linked list of property records to resolve a key, and every distinct property *name* permanently consumes a global property-key token that is never reclaimed. Both scale with observer count.

**Per-observer node properties** written by `write_neo4j_results.py`, all suffixed
with the observer's hex pubkey: `influence_`, `hops_`, `trusted_followers_`,
`trusted_reporters_`. All four are GrapeRank-run-fresh, NOT ingest-fresh — the
`FOLLOWS`/`MUTES`/`REPORTS` edges update within seconds of a kind 3/10000/1984
event, but these properties only change when that observer's GrapeRank job
lands. Prefer an edge match over `hops_<observer> = 1` when you need "does X
currently follow Y".

**One tier table, three expanders.** `_TIER_PREDICATES` is expanded by
`get_all_section_stats` (the `/stats` bucket counts), by `_build_tier_predicate`
(the `/connections?tier=…` filter), and by `_tier_case` (the per-row `tier` on
`/connections` items and the subject's own `tier` on `/overview`), so none of
them can disagree about which subject sits in which bucket. The same fallthrough
is also written in Python (`tier_thresholds.classify_tier`) for the GrapeRank
result writer, which buckets scorecards before they reach the graph and so has
no query to run; `tests/integration/test_tier_classifier_matches_cypher.py`
asserts the two agree. The band bounds come
from `_tier_band_params()` — the `TIER_*` constants, bound the same way by every
expander. No endpoint takes band overrides: a client that could move a band could
make `/connections?tier=…` return rows `/stats` counted in a different bucket.
Verified is strict `>` the
verified line, so `_VERIFIED_LINE` / `_UNVERIFIED_LINE` are exact complements
among subjects that have an influence at all (`_NO_INFLUENCE` is the third
case); let them drift and a subject sitting exactly on the line lands in no
bucket. `get_outbound_counts_and_influence` and
`get_paginated_flagged_connections` share the same `<= $verified_line` reading
of "flagged", which is why `/overview`'s `flagged_count` matches
`/connections?kind=flagged`.

Dynamic property access uses `user[$influence_key]` parameterization so
`influence_<observer>` columns don't get string-interpolated into Cypher.
Don't write the property name into the query string — a per-observer name in the
query text gives every observer its own Neo4j plan-cache entry. The write side
follows the same rule from the other direction: `write_neo4j_results.py` builds
the `{name: value}` map in Python and does `SET n += row.props`.

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
