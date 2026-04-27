# Brainstorm API

This is the HTTP API for Brainstorm

## How to run the project.

- docker run --name my-postgres-db -e POSTGRES_PASSWORD=postgrespw -e POSTGRES_USER=postgres -e POSTGRES_DB=brainstorm-database -p 5432:5432 -d postgres:alpine3.20

- poetry install

- poetry shell

- cp env.example .env

- poetry run alembic upgrade head

- poetry run uvicorn app.api:app --host 0.0.0.0

## How to create a DB migration.

First, modify the DB files

- poetry run alembic revision --autogenerate -m "Your new modification"

## How to check the API project's documentation

Access the URL `http://0.0.0.0:8000/docs`

## How to interact with the DB with an Admin Panel

Access the URL `http://0.0.0.0:8000/admin`

## How to check if the project is fine

- poetry shell

- poe check_all (this is a custom command that runs many checkers)

## How to run everything with docker

- docker network create brainstorm-network
- docker-compose up

## Admin routes (`/admin/*`)

All admin routes are gated by JWT auth + pubkey whitelist. Controlled by env vars:

```env
ADMIN_ENABLED=true                        # enable/disable all admin routes (default: false)
ADMIN_WHITELISTED_PUBKEYS=abc123,def456   # comma-separated pubkeys allowed access (empty = blocks all)
```

Requests must include a valid auth token (JWT) and the token's pubkey must be in the whitelist. When the whitelist is empty, all requests are blocked.

### Available admin routes

- `GET /admin/brainstormPubkey/{nostr_pubkey}` — trigger a GrapeRank calculation for any pubkey

## GrapeRank request rate limiting

Per-user throttling for `POST /graperank`. When enabled, a user cannot trigger a new calculation if their latest one is more recent than the configured window.

```env
BLOCK_FREQUENT_GRAPERANK_REQUESTS=false        # enable/disable the per-user cooldown (default: false)
BLOCK_FREQUENT_GRAPERANK_REQUESTS_MINUTES=30   # cooldown window in minutes (default: 30)
```

### Updating env vars

No image rebuild needed. Values are read at process startup, so a restart is enough:

1. Update values in `.env` or `docker-compose.yml`
2. Restart the server container:

```bash
# Local dev (brainstorm_server/docker-compose.yml) — service is "web"
docker-compose restart web

# Deployment (brainstorm_one_click_deployment/docker-compose.yml) — service is "brainstorm-server"
docker-compose restart brainstorm-server
```

**Only rebuild if you change code or dependencies:**

```bash
docker-compose build web && docker-compose up -d web
```

## Nsec encryption

Server-generated nsec keys are encrypted at rest using Fernet (AES-128-CBC + HMAC). The `brainstorm_nsec` table has two columns during the transition:

- `nsec` — plaintext (legacy, kept for safe rollback)
- `encrypted_nsec` — Fernet-encrypted version

Read path prefers `encrypted_nsec`, falls back to `nsec`. New rows dual-write.

### Key storage

The encryption key lives in a file, not env vars: `/run/secrets/nsec_encryption_keys` inside the container, bind-mounted **rw** from `./secrets/nsec_encryption_keys` on the host (see `brainstorm_one_click_deployment/docker-compose.yml`). File format: comma-separated Fernet keys, newest first.

The app loads the file into a mutable in-process `MultiFernet` at startup. Rotation rewrites the file and reloads in-place — no signals, no container restart.

### Bootstrap

On first startup, if the key file is missing or empty, the app auto-generates a fresh Fernet key, writes it to the file, and encrypts any pre-existing plaintext rows from the legacy `nsec` column. No manual bootstrap step required.

**Important:** after first bootstrap on a given host, copy `./secrets/nsec_encryption_keys` to your secrets vault. The key file is the only thing standing between `encrypted_nsec` ciphertexts and plaintext recovery — losing it (once the plaintext column is dropped) loses all server-held nsecs.

### Key rotation

Rotation is triggered via a whitelisted admin endpoint. Your pubkey must be in `admin_whitelisted_pubkeys` and `admin_enabled` must be true on the target server.

```
POST /admin/nsec-encryption/rotate    → 202 {"status": "started"}
POST /admin/nsec-encryption/verify    → 200 {"ok": N, "fail": 0}
```

`rotate` is fire-and-forget: it spawns an in-process background task and returns immediately. 409 is returned if a rotation is already running. Progress and errors land in the server logs — see _Later work_ below for the audit table follow-up.

Flow (in `app/services/nsec_encryption_service.py`):

1. Pre-flight: verify every `encrypted_nsec` row decrypts with the current key. Abort if any row fails — refuse to rotate over drifted state.
2. Generate new Fernet key. Atomic-write `<new>,<old>` to the key file, reload in-process. App now decrypts with either key.
3. Re-encrypt every row using the new key.
4. Verify every row decrypts with `<new>` alone.
5. Atomic-write `<new>` to the key file, reload. Rotation complete.

On any failure after step 2, the key file remains `<new>,<old>` so the app keeps working while you investigate.

**After rotation, manually copy the new key file to your secrets vault.**

### Rollback

Since the plaintext `nsec` column is preserved, rolling back is safe:

1. Revert code deploy.
2. Old code reads directly from `nsec` column, ignoring `encrypted_nsec`.

### Emergency: key lost

Back up the key file to a secrets vault after every rotation. As long as the key exists somewhere, nsecs are recoverable.

If truly lost while the `nsec` column still exists: clear `encrypted_nsec`, restart the server (the lifespan bootstrap will generate a fresh key and re-encrypt from the plaintext column). After the `nsec` column is dropped, nsecs become unrecoverable without the key.

### Later work

- **Audit / rotation history table** (`nsec_key_rotation`) — record started_at, finished_at, actor pubkey, status, rows_updated, error. Enables a status-polling endpoint for the rotation task.
- **Chunked re-encryption** — current implementation loads all rows at once; revisit if the table grows large.
- **Drop the plaintext `nsec` column** — separate migration once prod soak confirms `encrypted_nsec` is authoritative.

## GrapeRank preset params

Preset values live in the `graperank_preset` table — one row per preset (`DEFAULT`, `PERMISSIVE`, `RESTRICTIVE`). Seeded by migration with factory defaults. After deploy, the DB is the source of truth; admins edit values via the admin endpoint. Each write also appends to `graperank_preset_history` (append-only audit log).

Reads always hit the DB — no in-process cache. One small indexed PK lookup per brainstorm request creation; trivial cost vs. the rest of request creation. Multi-worker coherent by construction: every worker sees admin writes immediately after commit.

Two enums separate concerns: [`BuiltinPresetTemplate`](app/services/graperank_presets.py) (DEFAULT/PERMISSIVE/RESTRICTIVE) is used as the input type at admin endpoints and the user PUT body, so FastAPI rejects `CUSTOM` at validation. [`GrapeRankPresetTemplate`](app/services/graperank_presets.py) (the same three plus `CUSTOM`) is used for stored state and response schemas where `CUSTOM` is a real possibility.

Pydantic validates the wire shape (`GrapeRankPresetParams`, camelCase, `extra="forbid"` — typos fail fast).

The shape is the contract with the Java worker (`brainstorm_graperank_algorithm`). Java enforces it via Jackson — missing required fields throw and the request is marked `FAILED`.

### Tuning an existing preset (per-deployment)

```
PUT /admin/graperank/preset/{DEFAULT|PERMISSIVE|RESTRICTIVE}
Body: full GrapeRankPresetParams
```

Validates via pydantic, writes the row, appends a history entry with `change_type=UPDATE` and `changed_by=<admin pubkey>`. Subsequent brainstorm requests on any worker read fresh values from the DB.

History: `GET /admin/graperank/preset/{id}/history` (newest first, capped at 100).

### Adding a new param (schema change)

1. [`GrapeRankPresetParams`](app/services/graperank_presets.py) — add `newFieldName: float` (camelCase, matches Java).
2. [`db_models/__init__.py`](app/db_models/__init__.py) — add `new_field_name` column to both `GrapeRankPreset` and `GrapeRankPresetHistory`.
3. [`graperank_preset_repo.COLUMN_MAP`](app/repos/graperank_preset_repo.py) — add the camel↔snake mapping.
4. New alembic migration: `ALTER TABLE` both tables to add the column. Set initial value for existing rows.
5. `GET /user/graperank/presets` auto-includes the new field. No schema edits.

**Java (`brainstorm_graperank_algorithm`):**

6. `GrapeRankParams.java` — add `@JsonProperty(required = true) double newFieldName`.
7. `Constants.java` — add to `DEFAULT_PARAMS` (dev-only, not on prod path).
8. Wire into `GrapeRankAlgorithm.java`.

**Deploy order:** Java first, then Python. Old Java reading a payload with an unknown field is fine (`FAIL_ON_UNKNOWN_PROPERTIES=false`). New Java reading an old payload without the required field throws.

### Removing a param

1. Delete from `GrapeRankPresetParams`, both DB models, and `COLUMN_MAP`.
2. Migration: `ALTER TABLE ... DROP COLUMN` on both tables.
3. Java: delete from `GrapeRankParams` record + `Constants.DEFAULT_PARAMS` + algorithm.
4. Deploy Python first; Java ignores unknown fields.

### Adding a new preset (e.g. `EXPERIMENTAL`)

Static enum on purpose — adding a preset name is a code change, not a config change.

1. Add enum member to both `BuiltinPresetTemplate` and `GrapeRankPresetTemplate` (same string value).
2. New migration: insert row into `graperank_preset` with factory values + a `CREATE` history row.

### Historical request rows

`BrainstormRequest.graperank_params` JSON snapshots are frozen in the shape they were written with. They're never replayed — audit-only. Adding or removing fields does not require backfilling these.

## Scripts

Scripts live in `scripts/` and are excluded from the Docker image via `.dockerignore`. Run from your local machine.

### Setup

```bash
pip3 install httpx nostr-sdk
```

### `scripts/test_admin_brainstorm_pubkey.py`

Triggers a GrapeRank calculation for a pubkey via the admin endpoint. Authenticates with your nsec, then calls `GET /admin/brainstormPubkey/{pubkey}`.

```bash
# Your own pubkey (local)
python3 scripts/test_admin_brainstorm_pubkey.py

# Another user's pubkey (local) — accepts hex or npub1
python3 scripts/test_admin_brainstorm_pubkey.py --target-pubkey npub1...

# Against staging
python3 scripts/test_admin_brainstorm_pubkey.py --base-url https://brainstormserver-staging.nosfabrica.com --target-pubkey npub1...

# Against production
python3 scripts/test_admin_brainstorm_pubkey.py --base-url https://brainstormserver.nosfabrica.com --target-pubkey npub1...
```

Your nsec will be prompted securely (hidden input). Your pubkey must be in `admin_whitelisted_pubkeys` on the target server.
