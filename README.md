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

## Test scripts

Test scripts (`test_*.py`) are excluded from the Docker image via `.dockerignore` and run from your local machine.

### Setup

```bash
pip3 install httpx nostr-sdk
```

### `test_admin_brainstorm_pubkey.py`

Triggers a GrapeRank calculation for a pubkey via the admin endpoint. Authenticates with your nsec, then calls `GET /admin/brainstormPubkey/{pubkey}`.

**Usage:**

```bash
# Your own pubkey (local)
python3 test_admin_brainstorm_pubkey.py

# Another user's pubkey (local) — accepts hex or npub1
python3 test_admin_brainstorm_pubkey.py --target-pubkey npub1...

# Against staging
python3 test_admin_brainstorm_pubkey.py --base-url https://brainstormserver-staging.nosfabrica.com --target-pubkey npub1...

# Against production
python3 test_admin_brainstorm_pubkey.py --base-url https://brainstormserver.nosfabrica.com --target-pubkey npub1...
```

Your nsec will be prompted securely (hidden input). Your pubkey must be in `admin_whitelisted_pubkeys` on the target server.
