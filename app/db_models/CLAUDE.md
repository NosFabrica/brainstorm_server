# app/db_models

SQLAlchemy 2.0 ORM model definitions for PostgreSQL. Everything lives in
[`__init__.py`](__init__.py) — there are no per-table files (yet).

Read this once, then trust the imports. Tables are managed by Alembic (see
[../../alembic/CLAUDE.md](../../alembic/CLAUDE.md)) — **do not** call
`Base.metadata.create_all()`.

## Base and conventions

- `Base = DeclarativeBase + AsyncAttrs` — every model subclasses this.
- `TimestampMixin` — adds `created_at` / `updated_at` (server-side `func.now()` + `onupdate=func.now()`) and auto-derives `__tablename__` from the class name lowercased. Most tables use it; `GrapeRankPresetHistory` does not (history rows are immutable).
- Enums of values that show up as VARCHAR(128) columns (e.g. `BrainstormRequestStatus`) — model-side enums, not Postgres ENUMs. Cheap to add new states without a migration.

## Tables

### `BrainstormRequest` — `brainstorm_request`

The GrapeRank job ledger. One row per requested run.

| Column | Type | Notes |
|---|---|---|
| `private_id` | int PK | autoincrement |
| `password` | str(128) | random, `generate_secure_password` default — gates webhook callbacks |
| `status` | str(128) | `waiting \| ongoing \| success \| failure` (overall) |
| `status_ta_publication` | str(128) | same vocabulary, publishing-to-Nostr stage |
| `status_internal_brainstorm_publication` | str(128) | internal publication step, nullable |
| `count_values` | text | lightweight per-bucket counts (per-hop/confidence), used by admin lists. A snapshot of the run: bucketed against the verified line from that run's own `graperank_params`, so it stays comparable with what `/stats` showed at the time even if the observer later changes preset |
| `error` | JSONB | structured error info (algorithm code + message) when status==failure |
| `parameters` | text | JSON-encoded algorithm parameters |
| `algorithm` | str | "graperank", etc. |
| `pubkey` | str | observer pubkey the run was for |
| `graperank_preset_used` | str | `DEFAULT \| PERMISSIVE \| RESTRICTIVE \| CUSTOM` |
| `graperank_params` | JSONB | snapshot of the resolved 11-float params at run time |

Status transitions are owned by `brainstorm_request_repo` setters.

### `BrainstormNostrRelayTransfer` — `brainstorm_nostr_relay_transfer`

Per-kind cursor for the relay-sync workflow (see
[../nostr_event_transferer/CLAUDE.md](../nostr_event_transferer/CLAUDE.md)).

| Column | Type | Notes |
|---|---|---|
| `kind` | int, **UNIQUE** | the only kind we'd ever resume; uniqueness enforced |
| `completed` | bool | initial backfill done? |
| `oldest` | int (epoch s) | resume cursor for incremental sync |
| `events` | int | cumulative count for telemetry |
| `started_at` | float (epoch s) | when the current backfill kicked off |

### `BrainstormNsec` — `brainstorm_nsec`

Per-user state. Pubkey is the PK (one row per user).

| Column | Type | Notes |
|---|---|---|
| `pubkey` | str PK | the **user** pubkey (Nostr login) |
| `nsec` | str | legacy plaintext nsec for the brainstorm-assistant pubkey — kept for back-compat |
| `encrypted_nsec` | str | preferred storage (Fernet, see `app/utils/encryption.py`). Repo decrypts transparently. |
| `last_time_triggered_graperank` | DateTime | last time the user *asked* for a run |
| `last_time_calculated_graperank` | DateTime | last time a run *finished* (SUCCESS) |
| `graperank_preset` | str | `DEFAULT \| PERMISSIVE \| RESTRICTIVE \| CUSTOM` |
| `graperank_custom_params` | JSONB | the user's CUSTOM params (used only if `graperank_preset == "CUSTOM"`) |
| `last_published_pubkeys` | bytes (LargeBinary) | concatenated 32-byte hex pubkeys, no separator — see [../repos/CLAUDE.md](../repos/CLAUDE.md) for the safe accessors |
| `last_published_graperank_request_id` | int FK → `brainstorm_request.private_id` | back-link to the latest publishing job |

**Watch out**: the `nsec` *vs* `encrypted_nsec` duality is a migration artefact. New code should write `encrypted_nsec` and ignore `nsec`. The repo helpers handle the fallback for old rows.

### `ObserverWhitelist` — `observerwhitelist`

Compact per-observer trust list. One row per observer (`observer_pubkey` PK),
**overwritten each successful run**. Replaces parsing the huge `BrainstormRequest.result`
blob for `GET /whitelisted/{observer_pubkey}`.

| Column | Type | Notes |
|---|---|---|
| `observer_pubkey` | str PK | the observer whose perspective this whitelist is |
| `scores` | JSONB | `{observee_pubkey: influence}`, **above-cutoff only** (`round(influence,2) >= cutoff_of_valid_graperank_scores`), rounded influence |
| `last_request_id` | int FK → `brainstorm_request.private_id` | provenance: the run that produced this snapshot |

Written by the results consumer (`message_queue_tasks/message_queue_consumer.py`) in the
same transaction as the run's status. Read by `observer_whitelist_repo.py`.

### `GrapeRankPreset` — `graperank_preset`

11-float parameter row per builtin preset (DEFAULT / PERMISSIVE / RESTRICTIVE).
PK is the string id (e.g. `"DEFAULT"`). Seeded by migration; mutated only via
the admin endpoint.

### `GrapeRankPresetHistory` — `graperank_preset_history`

Append-only audit log. Same 11 params plus `change_type`, `changed_by`,
`changed_at`. No `updated_at` — history rows don't mutate. Repos in
`app/repos/graperank_preset_repo.py` enforce the append.

## Adding a new table

1. New `class Foo(TimestampMixin, Base): __tablename__ = "foo"` in `__init__.py`.
2. `alembic revision --autogenerate -m "add foo"` from the repo root.
3. Inspect the generated file in [../../alembic/versions/](../../alembic/versions/) — autogenerate misses things like CHECK constraints, index names, and enum changes. Hand-edit.
4. New repo file in [`app/repos/`](../repos/) (`foo_repo.py`). Don't put queries in services.

## Adding a new column to an existing table

1. Add `Mapped[type] = mapped_column(...)` to the existing class.
2. Generate the migration. Always set `server_default` on non-nullable columns so existing rows don't break the migration.
3. If the column is large, expose it through a `defer(...)` option in the repo, never select it by default.
