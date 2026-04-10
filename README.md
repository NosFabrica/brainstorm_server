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

The encryption key lives in a file, not env vars: `/run/secrets/nsec_encryption_keys` inside the container, bind-mounted from `./secrets/nsec_encryption_keys` on the host (see `brainstorm_one_click_deployment/docker-compose.yml`). File format: comma-separated Fernet keys, newest first. Missing or empty file → encryption disabled (passthrough).

The app reads and caches the key on first use. A SIGUSR1 signal clears the cache so the next call rereads the file — this is how rotation achieves zero downtime.

### Bootstrap

```bash
cd brainstorm_one_click_deployment
mkdir -p secrets
docker compose up -d brainstorm-server
docker compose --profile ops run --rm nsec-rotate
```

The rotation script detects the missing key file, generates one inside the container, writes it to `./secrets/nsec_encryption_keys` via the bind mount, signals the app to pick it up, and encrypts any existing rows.

### Key rotation

```bash
cd brainstorm_one_click_deployment
docker compose --profile ops run --rm nsec-rotate
```

Flow (automated by `scripts/rotate_nsec_key.py`):

1. Read current key from file.
2. Generate new Fernet key.
3. Write `<new>,<old>` to file, send SIGUSR1 to `brainstorm-server`. App now decrypts with either key.
4. Re-encrypt every row using the new key.
5. Verify every row decrypts with the new key alone.
6. Write `<new>` to file, send SIGUSR1 again. Old key is gone.

Zero downtime — no container recreate. On any failure before step 6, the file still has both keys and the app still works.

Flags: `--dry-run`, `--verify-only`, `--container NAME`, `--batch-size N`.

### Rollback

Since plaintext `nsec` column is preserved, rolling back is safe:
1. Revert code deploy.
2. Old code reads directly from `nsec` column, ignoring `encrypted_nsec`.

### Emergency: key lost

Back up the key file (password manager / secrets vault). As long as the key exists somewhere, nsecs are recoverable.

If truly lost while `nsec` column still exists: clear `encrypted_nsec`, write a new key file, re-run the rotation script to re-encrypt from the plaintext column. After the `nsec` column is dropped, nsecs become unrecoverable without the key.

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

### `scripts/rotate_nsec_key.py`

Bootstraps and rotates the nsec encryption key. See [Nsec encryption](#nsec-encryption) above.

### `scripts/test_seed_users.py`

Seeds 10 test Nostr users with follow, mute, and report relationships to a local relay.

```bash
python3 scripts/test_seed_users.py [--relay ws://localhost:7777]
```
