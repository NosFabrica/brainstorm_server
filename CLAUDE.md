# Brainstorm Server

FastAPI service that backs the Brainstorm app. It ingests Nostr events,
maintains a follow/mute/report graph, drives GrapeRank trust-score
computations, and exposes a profile search backed by Vespa.

## What it talks to

| Service | Role |
|---|---|
| **PostgreSQL** | Domain DB. Alembic migrations in `alembic/`. Schema = `app/db_models/`. |
| **Neo4j** | Graph DB. Stores follow/mute/report relationships per `NostrUser` pubkey. |
| **Redis** | Message queue (between strfry plugin → kind-event consumer, and between graperank workers → score uploader) **and** reverse-set caches (`followed_by:<pubkey>`, `muted_by:<pubkey>`, `reported_by:<pubkey>`). |
| **Vespa** | Search engine for Nostr profiles. Per-observer trust scores live in a sparse `quality_scores` tensor on each doc, keyed by observer pubkey. See `app/core/vespa.py`. |
| **strfry / neofry** | Local Nostr relays. The server publishes signed TA (Trusted Assertions) events to them and consumes incoming events. |
| **Nostr SDK** | `nostr_sdk` Python bindings, used for parsing pubkeys (npub ↔ hex), event signing, and relay clients. |

## Three key flows worth knowing

### 1. Kind-0 (profile metadata) ingestion
`Redis queue strfry:events` → [`message_queue_consumer.consume_strfry_plugin_messages`](app/message_queue_tasks/message_queue_consumer.py) → [`process_strfry_event`](app/message_queue_tasks/process_strfry_event.py) → for kind 0, [`vespa.upsert_profile(pubkey, profile)`](app/core/vespa.py). Partial update with `create=true`: existing tensor cells (scores) are preserved; missing kind-0 fields are cleared to `""`.

### 2. GrapeRank score upsert + Nostr publishing
`Redis queue nostr_results_message_queue` → [`consume_nostr_upload_messages`](app/message_queue_tasks/message_queue_consumer.py) → [`process_nostr_upload_message`](app/message_queue_tasks/upload_nostr_events.py) → signs TA events, publishes to relays, and calls [`upsert_scores_to_vespa`](app/message_queue_tasks/upload_nostr_events.py) → [`vespa.batch_upsert_scores`](app/core/vespa.py) which fans out N concurrent POSTs (bounded at `_BATCH_CONCURRENCY = 32`). Scores are mirrored for **every** observer (keyed by the observer's pubkey in the `quality_scores` tensor), not just `settings.periodic_graperank_pubkey`.

Failure policy: Vespa is a search mirror. The Nostr publish is the source of truth — Vespa failures are logged but never propagate.

### 3. Profile search
`GET /search/byText?text=...&onlyRanked=...&observerPubkey=...` → [`router.search_by_text_endpoint`](app/routers/search/router.py) → if input is hex pubkey or npub, direct `vespa.get_document(pubkey)`; otherwise `vespa.search(...)` against the `name_and_quality_score_only` rank profile. Observer perspective = the optional `observerPubkey` query param (hex or npub; unresolvable values fall back silently), else `settings.periodic_graperank_pubkey` (which itself falls back to a hardcoded default in the router if unset).

## Directory map

Each directory has its own `CLAUDE.md`. Start there before diving in.

| Path | What's inside |
|---|---|
| [`app/api.py`](app/api.py) | FastAPI app + lifespan. Spawns all the background asyncio tasks. |
| [`app/core/`](app/core/CLAUDE.md) | Shared infra: config, vespa client, redis, neo4j driver, logging. |
| [`app/cronjobs/`](app/cronjobs/CLAUDE.md) | Time-based recurring tasks (stale-job sweep, periodic graperank trigger). |
| [`app/db_models/`](app/db_models/CLAUDE.md) | SQLAlchemy models (PostgreSQL). |
| [`app/message_queue_tasks/`](app/message_queue_tasks/CLAUDE.md) | Redis-driven consumers + the strfry-event dispatcher. |
| [`app/models/`](app/models/CLAUDE.md) | Pydantic models for in-memory data (e.g. `GrapeRankResult`). |
| [`app/neo4j_db/`](app/neo4j_db/CLAUDE.md) | Neo4j driver bootstrap. Queries live in `app/repos/user_repo.py`. |
| [`app/nostr_event_transferer/`](app/nostr_event_transferer/CLAUDE.md) | Initial-sync + incremental sync between Nostr relays. |
| [`app/repos/`](app/repos/CLAUDE.md) | Data-access layer (one module per domain table). |
| [`app/routers/`](app/routers/CLAUDE.md) | FastAPI route modules. Aggregated in `routers/router.py`. |
| [`app/schemas/`](app/schemas/CLAUDE.md) | Pydantic request/response schemas exposed at API boundaries. |
| [`app/services/`](app/services/CLAUDE.md) | Business-logic services. |
| [`app/utils/`](app/utils/CLAUDE.md) | Auth dependency, encryption, NIP-98, small helpers. |
| [`alembic/`](alembic/CLAUDE.md) | DB migration scripts. |
| [`scripts/`](scripts/CLAUDE.md) | One-off CLI tools (admin token, smoke tests). |
| `start.sh` | Entrypoint: `alembic upgrade head && uvicorn app.api:app`. |
| `docker-compose.yml` | Local stack (postgres, redis, neo4j, strfry, neofry). |
| `env.example` | All env vars. Copy to `.env` and fill in. |

## Conventions

- **Everything is async** — FastAPI endpoints, DB calls (`asyncpg`, `neo4j.AsyncDriver`, `redis.asyncio`), and Vespa HTTP calls. Don't introduce blocking I/O.
- **Settings via Pydantic** in `app/core/config.py`. Add a `Field(...)` for required vars, `Field(default=...)` for optional. Mirror in `env.example`.
- **Background tasks are spawned in the FastAPI lifespan** (`app/api.py`) with `asyncio.create_task` and cancelled in the `finally` block. Add new long-running consumers there.
- **Loggers**: `from app.core.loggr import loggr; logger = loggr.get_logger(__name__)`. Don't use `print` or stdlib `logging` directly.
- **Vespa is best-effort**: writes are mirrored from Nostr/PostgreSQL/Neo4j — failures get logged, don't get raised. The reverse isn't true: the *graph* and *Postgres* are source-of-truth and their writes must succeed.

## Vespa specifics

- The application package (schema, services.xml, hosts.xml) lives in a separate repo: `brainstorm_one_click_deployment/vespa-app/`. The brainstorm-server speaks only HTTP to a running Vespa via `settings.vespa_url`.
- **Per-observer scores** = cells in the `quality_scores` sparse tensor `tensor<int8>(user{})`. The cell address is the observer's pubkey (hex). Upsert = `add` op; delete = `remove` op.
- **Profile partial updates** = `POST /document/v1/doc/doc/docid/<pubkey>?create=true` with `{"fields": {field: {"assign": value}}}`. The `create=true` makes it act like an upsert.
- **Search ranking** uses `name_and_quality_score_only`. The query passes `ranking.features.query(user_q)={<observer>:1.0}` so the rank profile picks that observer's score from the tensor.
- The shared `httpx.AsyncClient` in `app/core/vespa.py` is initialized lazily and closed in the FastAPI lifespan shutdown via `vespa.aclose()`. **Don't open ad-hoc httpx clients elsewhere for Vespa calls** — reuse this one.

## Common tasks

| I want to… | Look at… |
|---|---|
| Add a new Vespa field | `brainstorm_one_click_deployment/vespa-app/schemas/doc.sd` + update `vespa.upsert_profile` if it's a profile field |
| Change the search rank profile | `vespa-app/schemas/doc.sd` (rank-profile blocks). Re-deploy needed but no re-feed unless you added a field |
| Change profile-field clear behaviour | `vespa.upsert_profile` (currently assigns `""` when the new event omits the field) |
| Adjust score upsert concurrency | `_BATCH_CONCURRENCY` in `app/core/vespa.py` |
| Add a new redis consumer | `app/message_queue_tasks/` + spawn the task in `app/api.py` lifespan |
| Add an HTTP endpoint | Create a router under `app/routers/<topic>/router.py`, register it in `app/routers/router.py` |
| Add an env var | `app/core/config.py` + `env.example` + the relevant compose file |
| Add a PostgreSQL migration | `alembic revision --autogenerate -m "..."` (after model change in `app/db_models/`) |

## Run locally

```bash
cp env.example .env             # then fill in DB creds, neo4j password, vespa URL, etc.
docker compose up -d            # postgres, redis, neo4j, strfry, neofry
./start.sh                      # runs alembic migrations then uvicorn
```

Vespa is **not** in this compose — it lives in `brainstorm_one_click_deployment`. Either point `VESPA_URL` at a Vespa you run from that repo, or at a remote deploy.

## Things to know (gotchas)

- **Prod timestamptz drift.** A few **production** timestamp columns are `timestamp WITH time zone` while migrations/staging/local are naive (`brainstorm_request.*`, most of `brainstorm_nsec.*`, `brainstorm_nostr_relay_transfer.*` — but NOT `last_time_published_graperank`, `scheduling`, `graperank_preset*`, `observerwhitelist`). asyncpg returns tz-aware datetimes from those, so Python-side subtraction against naive `datetime.now()` throws `can't subtract offset-naive and offset-aware datetimes` **on prod only**. Normalize aware→naive at any such boundary. Full column table, root cause, patched sites, and re-verify commands: [`docs/timestamptz-drift.md`](docs/timestamptz-drift.md).

- **`periodic_graperank_pubkey`** is the default observer perspective for `/search/byText` when no `observerPubkey` is passed. Score mirroring to Vespa is **not** gated on it — `upsert_scores_to_vespa` runs for every observer. If `periodic_graperank_pubkey` is empty, search still works using the hardcoded default in the search router.
- **`MAX_QUERY_WORDS = 6`** in `vespa.py` caps how many words of the search query get treated as separate match groups. Longer queries are truncated.
- **`VESPA_FULL_SYNC` / `RELAY_FULL_SYNC`** (in `app/core/config.py`, both default `False`) re-push every above-cutoff scorecard on each graperank run when `True`. Delta is the steady state; drift is repaired by `FULL_SYNC_EVERY_N_RUNS` + the admin resync.
- **Vespa needs `about_gram` to exist in the schema** for partial bio matches ("nosfab" → "nosfabrica") to work. If you wipe the volume and re-deploy, you also need to refeed (or `vespa reindex`) so the derived `about_gram` field gets populated.
- **The brainstorm-server's docker-compose.yml** does NOT include Vespa. Vespa is provided by the sibling `brainstorm_one_click_deployment` repo. Set `VESPA_URL` accordingly.
