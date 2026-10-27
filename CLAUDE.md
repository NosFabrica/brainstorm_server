# Brainstorm Server

FastAPI service that backs the Brainstorm app. It ingests Nostr events,
maintains a follow/mute/report graph, drives GrapeRank trust-score
computations, and publishes the scores to nostr as Trusted Assertions.

The wire format this server publishes — kind-30382 Trusted Assertions and their
kind-10040 designation — is specified in
[`NosFabrica/protocols`](https://github.com/NosFabrica/protocols)
([trusted-assertions.md](https://github.com/NosFabrica/protocols/blob/main/specs/trusted-assertions.md);
the GrapeRank computation in
[graperank.md](https://github.com/NosFabrica/protocols/blob/main/specs/graperank.md)).
The map of the estate's repos and deployments is
[ECOSYSTEM.md](https://github.com/NosFabrica/protocols/blob/main/ECOSYSTEM.md) there.

## What it talks to

| Service | Role |
|---|---|
| **PostgreSQL** | Domain DB, including the share-link records (`short_url`). Alembic migrations in `alembic/`. Schema = `app/db_models/`. |
| **Neo4j** | Graph DB. Stores follow/mute/report and kind-10040 designation (`DESIGNATES`) relationships per `NostrUser` pubkey. |
| **Redis** | Message queue (between strfry plugin → kind-event consumer, and between graperank workers → score uploader), reverse-set caches (`followed_by:<pubkey>`, `muted_by:<pubkey>`, `reported_by:<pubkey>`), the forward `designates:<pubkey>` sets (kind-10040 designations, [ADR 0005](docs/adr/0005-designations-are-graph-edges.md)), and rate-limit counters (including the shortener's — its *records* live in PostgreSQL, see [`app/routers/shorturl/`](app/routers/shorturl/CLAUDE.md)). |
| **Search relay** | The shared vespa-relay (`SEARCH_RELAY_URL`). Mirrors the 30382s we publish and answers `POST /search/pubkeys` (public, no capability document); our ORE-05 proxies to it, and ORE-04 `merchants` reads kind-30402 listings from it over NIP-FE. See `app/core/search_relay.py`. |
| **strfry / neofry** | Local Nostr relays. The server publishes signed TA (Trusted Assertions) events to them and consumes incoming events. |
| **Nostr SDK** | `nostr_sdk` Python bindings, used for parsing pubkeys (npub ↔ hex), event signing, and relay clients. |

## Two key flows worth knowing

### 1. GrapeRank score publishing
`Redis queue nostr_results_message_queue` → [`consume_nostr_upload_messages`](app/message_queue_tasks/message_queue_consumer.py) → [`process_nostr_upload_message`](app/message_queue_tasks/upload_nostr_events.py) → signs TA events and publishes them to the relays. The search relay picks the scores up from there; the server writes to no search index.

### 2. Profile search
Profile search is served by the shared search relay (`wss://search.brainstorm.world`, vespa-relay), not this server. The only search endpoint left here is ORE-05 `POST /search/pubkeys` ([`open_ranking/search.py`](app/routers/open_ranking/search.py)), which forwards to the relay's public `/search/pubkeys` backend ([`app/core/search_relay.py`](app/core/search_relay.py), `SEARCH_RELAY_URL`) and returns its status, body, `X-Reason` and `Retry-After` unchanged. The relay validates, ranks and gates the pov; we name our House Observer (`default_observer_pubkey()`) as `observer` on every forwarded body (the relay keeps none), force the NWT signer as `pov` when auth is on, and answer 503 + `X-Reason` if the relay is unreachable or off-spec. A pov whose 10040 names a rank service gets 200, with empty results while its scores are still mirroring; no 10040 is a 422. We cache nothing, so an empty answer isn't held for its `ttl`. With auth on, a global `relevance` search still gets the House view: the relay ignores `pov` for global algorithms (ORE-01), deliberately. We are the only Open Ranking provider (the relay has no capability document).

ORE-04 `POST /recommend/pubkeys` ([`open_ranking/recommend.py`](app/routers/open_ranking/recommend.py)) recommends **merchants**: every distinct author of the kind-30402 listings the relay returns for `sort:rank observer:<pov>`, ranked by the pov's GrapeRank influence — the number ORE-03 gives — and only then cut to `limit`, because the relay's order is by match tier and a coarse 0..100 rank. The relay's gate is the only trust filter: a seller our graph has no score for (a stale card, or a pov ranked by another provider) stays, last, in the relay's order. Algorithms `merchants` (House view), `merchants-pov`, and the `topic: true` pair `merchants-topic` / `merchants-topic-pov`; the first two ignore a topic, as a global algorithm ignores a pov. A topic loses its quotes (an unclosed one would swallow the lens tokens) and any extension-shaped word (`include:spam`). Reads go through two NIP-FE slots (the relay's per-client cap) and an answer is reused for 5 minutes in-process.

## Directory map

Each directory has its own `CLAUDE.md`. Start there before diving in.

| Path | What's inside |
|---|---|
| [`app/api.py`](app/api.py) | FastAPI app + lifespan. Spawns all the background asyncio tasks. |
| [`app/core/`](app/core/CLAUDE.md) | Shared infra: config, search-relay client, redis, neo4j driver, logging. |
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
| [`BIBLE.md`](BIBLE.md) | The narrative layer — the life of a Brainstorm request, store truth-rankings, keys and identity, what's built, design decisions. Read it to onboard. |
| [`CONTEXT.md`](CONTEXT.md) | The domain glossary (ubiquitous language): TA, Observer/Observee, Influence vs Rank, the request lifecycle. Read it before naming anything. |
| [`engineering-team/`](engineering-team/) | Book-of-work artifacts (stories, ADRs, test plans, reviews, audits), run per tapestry's engineering harness. **Strictness: Light (trial)** — gates and lanes in [`workflows/light-profile.md`](engineering-team/workflows/light-profile.md); one Standard control book first, per its trial protocol. Loose ends: `engineering-team/OPEN.md`. |
| [`docs/`](docs/) | Working docs: the (historical) internal-Vespa search essays and ADRs (`docs/adr/`). |
| `start.sh` | Entrypoint: `alembic upgrade head && uvicorn app.api:app`. |
| `docker-compose.yml` | Local stack (postgres, redis, neo4j, strfry, neofry). |
| `env.example` | All env vars. Copy to `.env` and fill in. |

## Conventions

- **Everything is async** — FastAPI endpoints, DB calls (`asyncpg`, `neo4j.AsyncDriver`, `redis.asyncio`), and outbound HTTP. Don't introduce blocking I/O.
- **Settings via Pydantic** in `app/core/config.py`. Add a `Field(...)` for required vars, `Field(default=...)` for optional. Mirror in `env.example`.
- **Background tasks are spawned in the FastAPI lifespan** (`app/api.py`) with `asyncio.create_task` and cancelled in the `finally` block. Add new long-running consumers there.
- **Loggers**: `from app.core.loggr import loggr; logger = loggr.get_logger(__name__)`. Don't use `print` or stdlib `logging` directly.
- **Client tag**: every event we sign carries `client_tag()` ([`app/utils/client_tag.py`](app/utils/client_tag.py)) — add it to any new `EventBuilder`.

## Common tasks

| I want to… | Look at… |
|---|---|
| Add a new redis consumer | `app/message_queue_tasks/` + spawn the task in `app/api.py` lifespan |
| Add an HTTP endpoint | Create a router under `app/routers/<topic>/router.py`, register it in `app/routers/router.py` |
| Add an env var | `app/core/config.py` + `env.example` + the relevant compose file |
| Add a PostgreSQL migration | `alembic revision --autogenerate -m "..."` (after model change in `app/db_models/`) |

## Run locally

```bash
cp env.example .env             # then fill in DB creds, neo4j password, etc.
docker compose up -d            # postgres, redis, neo4j, strfry, neofry
./start.sh                      # runs alembic migrations then uvicorn
```

## Deploying to staging

CI builds an image per branch of this repo; staging pins one of those tags. The
branch/PR/pin workflow — including whether to join the current staging branch
or start a new cycle — is documented in
[brainstorm-k8s `docs/staging-workflow.md`](https://github.com/NosFabrica/brainstorm-k8s/blob/master/docs/staging-workflow.md).

## Things to know (gotchas)

- **Prod timestamptz drift.** A few **production** timestamp columns are `timestamp WITH time zone` while migrations/staging/local are naive (`brainstorm_request.*`, most of `brainstorm_nsec.*`, `brainstorm_nostr_relay_transfer.*` — but NOT `last_time_published_graperank`, `scheduling`, `graperank_preset*`, `observerwhitelist`). asyncpg returns tz-aware datetimes from those, so comparing them against naive `utc_now()` throws `can't subtract offset-naive and offset-aware datetimes` **on prod only**. Pass DB values through `as_naive_utc()` (`app/utils/datetimes.py`) at any such boundary.

- **`RELAY_FULL_SYNC`** (in `app/core/config.py`, default `False`) re-pushes every above-cutoff scorecard on each graperank run when `True`. Delta is the steady state; drift is repaired by `FULL_SYNC_EVERY_N_RUNS` + the admin resync.
