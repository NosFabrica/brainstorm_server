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

`rotate` is fire-and-forget: it spawns an in-process background task and returns immediately. 409 is returned if a rotation is already running. Progress and errors land in the server logs — see *Later work* below for the audit table follow-up.

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

## Adding / removing a GrapeRank preset param

Preset params (`rigor`, `attenuationFactor`, etc.) are defined in Python and consumed by the Java worker (`brainstorm_graperank_algorithm`). Python is the source of truth. Java enforces the contract via Jackson — missing required fields throw.

### Add a new param

**Python (this repo):**

1. [`app/services/graperank_presets.py`](app/services/graperank_presets.py)
   - Add `newFieldName: float` to `GrapeRankPresetParams` (camelCase, matches Java).
   - Add the value for each entry in `PRESET_DEFINITIONS` (DEFAULT, PERMISSIVE, RESTRICTIVE).
2. `GET /user/graperank/presets` auto-includes the new field in its response. No schema edits.

**Java (`brainstorm_graperank_algorithm`):**

3. `src/main/java/.../grape/GrapeRankParams.java`
   - Add `@JsonProperty(required = true) double newFieldName` to the record. Same camelCase name as Python.
4. `src/main/java/.../grape/Constants.java`
   - Add a `DEFAULT_NEW_FIELD_NAME` constant.
   - Pass it to the `DEFAULT_PARAMS` constructor.
5. Wire the new field into the algorithm (likely `GrapeRankAlgorithm.java`).

**Deploy:**

- Deploy Java first, then Python — or both together. Old Java reading a payload with an unknown field is fine (`FAIL_ON_UNKNOWN_PROPERTIES=false`). New Java reading an old payload without the required field will throw and mark the request `FAILED` — avoid by deploying Java at or before Python.

### Remove a param

1. Delete from `GrapeRankPresetParams` + every entry in `PRESET_DEFINITIONS`.
2. Delete from `GrapeRankParams` record + `Constants.DEFAULT_PARAMS` + algorithm call sites.
3. Deploy Python first so new messages stop carrying the field. Java ignores unknown fields, so old-Java + new-Python is safe until Java catches up.

### Historical rows

Old `BrainstormRequest.graperank_params` JSON snapshots are frozen in the shape they were written with. They are never replayed — they're audit-only. Adding or removing fields does not require a DB migration.

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

