# scripts

Standalone admin / test scripts. Run them with `python -m scripts.<name>` from the repo root (so `app.*` imports resolve).

## Scripts

### `get_admin_token.py`

Mints a JWT for an admin pubkey **without** going through the Nostr auth
challenge. Useful for curling `/admin/*` endpoints during development.

- Reads `settings.jwt_secret_key` and an admin pubkey from `settings.admin_whitelisted_pubkeys` (or CLI arg — check the file).
- Prints the bearer token. Pipe it into a curl `Authorization: Bearer ...`.
- **Don't ship this to prod** — anyone with shell access could mint admin tokens. (Fine in dev because shell access already implies game over.)

### `test_admin_brainstorm_pubkey.py`

End-to-end smoke test for `/admin/brainstormPubkey/{pubkey}`:
- Mints an admin token.
- POSTs to the create/trigger endpoint.
- Asserts the response shape and prints the resulting brainstorm pubkey.

Run after schema changes to `BrainstormNsec` or the create-or-get flow.

### `test_admin_nsec_encryption.py`

Smoke test for `/admin/nsec-encryption/rotate` + `/verify`:
- Mints an admin token.
- Calls `POST /verify` and prints `{ok, fail}`.
- Triggers `/rotate` and polls.

Run after touching `app/services/nsec_encryption_service.py` or `app/utils/encryption.py`.

### `emit_flash_webhook.py`

Signs a Flash webhook and posts it at a running server. There is no Flash
sandbox, so this is how the receiving path gets exercised without paying for a
subscription. Stdlib-only and app-free, so it runs against a deployed host with
no `.env`; `tests/test_flash_webhook.py` asserts its HMAC matches the server's.

Pin `--at` to repeat a delivery byte-for-byte and see the retry path
(`"duplicate": true`); `--skew` a long way to see the replay window reject it;
`--tamper` to see a signature mismatch.

### `check_flash_credentials.py`

Confirms both Flash credentials work, independently, so a failure says which
one. Used at step 4 of a rotation — see
[`../docs/flash/credential-rotation.md`](../docs/flash/credential-rotation.md).
Exit code is 0 only if everything checked passed. Also stdlib-only.

### ORE-05 search helpers (`search_open_ranking.*`)

Ad-hoc tools for poking ORE-05 search against a **deployed** host
(default: staging). They need **no `.env`**; override the target via
`BRAINSTORM_HTTP`. Output is `{pubkey, rank}` only (no names).

- **`search_open_ranking.sh "<query>" [limit] [--pov <hex>] [--algo <id>]`** —
  ORE-05 `POST /search/pubkeys`, **unsigned** (ORE-05 auth is optional and
  currently off). Stdlib-only. `--pov` only matters with a *personalized*
  algorithm, so it auto-selects `relevance-pov` (the default `relevance` is
  global and ignores pov per ORE-01).
- **`search_open_ranking.py --query <q> [--pov <hex>] [--algo <id>] [--sign|--nsec ...]`**
  — same endpoint, but can **sign** an NWT; with server auth on, the signer is forced as `pov` (auto `relevance-pov`). Observer
  priority: signer (when signed) → `--pov` (auto `relevance-pov`) → server
  default. Needs `requests` + `nostr-sdk` (poetry env), like
  `smoke_open_ranking.py`; no `.env` required.

### `trigger_graperank_all.py`

Bulk-re-runs GrapeRank for **every** observer in `brainstorm_nsec` — the backfill
lever for re-asserting every observer's published TAs (e.g. after a new tag).
GrapeRank is expensive per observer, so:

- `--rate N` enqueues/min, `--limit N` cap per run — tune to the worker's throughput.
- Resumable + observable via the `brainstorm_request` table (no fragile state):
  a campaign starts at time T (stored in `--state-file`); an observer is
  *triggered* if it has a `graperank` request since T and *completed* when that
  request hits `success`. Re-running skips already-triggered observers.
- `--status` (counts, no writes), `--dry-run` (preview). Run **inside a
  brainstorm-server pod** (needs `.env` + DB + the graperank worker consuming).

Note: the default observer alone covers anonymous/default search; this is for
refreshing ALL personalized perspectives at once.

### `backfill_designations.py`

Replays every kind-10040 neofry holds through the live ingest handler
(`process_event_kind_10040`), building `DESIGNATES` edges + `designates:` sets for
designations published before that handler shipped. Reads a `strfry scan` NDJSON
(`--scan -` for stdin; the docstring has the docker/kubectl lines), keeps each
author's newest. Dry-run by default (author + designation counts); `--apply`
writes, serially. Idempotent — each author's set is replaced whole.

### `backfill_event_taggings.py`

Replays every kind-39999 neofry holds through the live handler
(`process_event_kind_39999`), for the event taggings and tagging headers that
were dropped at parse before their parsers shipped (ADR `trusted-lists/0002`
D10). Reads a `strfry scan` NDJSON (`--scan -`; the docstring has the
docker/kubectl lines). Dry-run by default (counts per shape, unreadable lines);
`--apply` writes. Idempotent — every upsert is latest-wins. Run it after the
release with the parsers is serving. Production runs are confirmed with the
cluster operators first.

### `test_admin_trusted_lists.py`

Smoke test for `POST /admin/trustedLists/{observer}`. `--seed` pushes a tag
element, a pubkey tagging and an event tagging (an article by coordinate, so no
lookup relay is needed) through the real redis ingest; `--verify-relay` reads
the Observer's 30392/30393/30394 lists back. The asserters' Influence is seeded where the
server reads it: the Scorecard database with `SCORES_IN_POSTGRES`, else Neo4j.

### `clean_trusted_reporters_props.py`

Strips leftover `trusted_reporters_<observer>` props from `NostrUser` nodes (the
reporter count is computed live, so they are inert). Observer keys come from
`db.propertyKeys()`. Dry-run by default (property + node counts); `--apply`
removes in node-id chunks of `--batch`, one transaction each; idempotent. Space is reused, but store
files only shrink after dump/reload. Held by
`tests/integration/test_clean_trusted_reporters_props_integration.py`.

### `zero_compromised_ta.py`

Zeroes the scores of the accounts in `compromised_pubkey` now, with no GrapeRank
run, through `compromised_service.zero_compromised` — what `PUT
/admin/compromised/{pubkey}` already does in the background, here with
per-observer output and an exit code. Every Observer holding a 30382 about the
account (per the TA relay, or `last_published_pubkeys` when the relay can't be read)
re-signs it at rank 0;
its Neo4j scores and whitelist entries are cleared. Dry-run by default;
`--apply` publishes; `--pubkey` for one account, `--observer <hex>` for one
Observer (TA only); `--db-only` skips the relay read. Exit 1 when a relay read
or publish didn't complete. Run in each environment's brainstorm-server pod.
Idempotent — the pipeline keeps the account at 0 on every later run anyway.

### `backfill_user_ids.py`

Gives every existing `NostrUser` its `user_id` (the id rule: ids committed in the Scorecard
database first, then written to Neo4j). Needs `SCORECARD_DB_URL`. Dry-run by default (counts);
`--apply` reads nodes without an id in pubkey order (`--page`), and writes ids back in
transactions of `--batch` (default `neo4j_write_batch_size`), creating the `pubkey` and `user_id`
uniqueness constraints first. Resumable and idempotent: only nodes without an id are touched.
On heavy nodes each write walks the property list: keep `--batch` small, watch the rows per
second it prints, and never run it alongside a large score write. Nodes whose pubkey is not
64-char lowercase hex are skipped and reported; they can never have an id.

- `--status`: nodes total / with / without an id, malformed, rows in `nostr_user`, ids no node carries.
- `--verify`: exit 1 if a well-formed node lacks an id, or a node's id disagrees with `nostr_user`.
- `--reconcile [--apply]`: after a restore of the Scorecard database. Puts back every
  `(user_id, pubkey)` the graph has and the table lost and moves the sequence past the graph's
  highest id. An id that differs between the two, or that the table has since given to another
  pubkey, stops it with exit 2 and nothing written.

Held by `tests/integration/test_backfill_user_ids_integration.py`, which only runs on an empty graph.

### `copy_scores_to_postgres.py`

The one-time copy of every Observer's stored scores from Neo4j properties into the `scorecard`
table, for the cutover to `SCORES_IN_POSTGRES`. **Reads Neo4j and never writes it.** Needs
`SCORECARD_DB_URL`, every node backfilled with its `user_id`, and the scheduler paused: it refuses
to run while any run is in the pipeline (`--force` overrides). Dry-run by default.

`--apply` sweeps the user nodes once in pubkey order (`--page` nodes per read, default 200),
reading each node's whole property map, so all Observers are copied in one pass. Each page's rows,
their per-Observer count and sums, and the cursor commit together, so an interrupted copy resumes
where it stopped and counts nothing twice. `trusted_reporters_` properties are ignored; a NaN or
infinite Influence is stored as none and counted; scores on a node without a `user_id` cannot be
copied and are counted.

- `--status`: Observers ever stored, nodes read of total, Observers and rows copied, the two counts above.
- `--verify`: exit 1 with the reasons unless the copy is ready for the switch: the sweep finished;
  it met no score on a node without a `user_id`; every Observer's stored rows have the count and
  sums recorded while reading (an Observer with stored rows and no record fails too); no Observer
  has a request since the last read began; no run is in the pipeline. Reads only Postgres, in
  seconds. It does not compare rows with Neo4j: `compare_reads` covers what the reads return.
- `--only <observer> --apply`: redo one Observer (its rows are replaced in one transaction).
- `--catch-up [--apply]`: redo every Observer with a request since the last complete read of
  Neo4j began (the sweep, or the catch-up before; `--since <UTC time>` overrides). One walk of the
  nodes in which only those Observers' scores leave the database. This is what keeps the pause
  short: sweep with `--force` while runs go on, pause, catch up, verify. Their rows are deleted
  first, so run it before anything reads `scorecard`.
- `--restart --apply`: forget progress and checksums; `scorecard` rows stay.

It keeps its progress in two small tables it creates in the Scorecard database
(`score_copy_progress`, `score_copy_checksum`); drop them once the cutover is settled.
Held by `tests/test_copy_scores_parsing.py` and `tests/integration/test_copy_scores_integration.py`.

### `compare_reads.py`

The cutover read comparison: the same Scorecards must answer the same reads before and after
`SCORES_IN_POSTGRES` flips. Take both records with ingest held (`ingest:hold`, see
`app/message_queue_tasks/CLAUDE.md`).

Reads in-process, no HTTP and no tokens: it calls the services the routers call
(`user_service`, `network_alerts_service`, `graph_service`, `resolve_verified_cutoffs`) with the
Observer passed explicitly, and serialises each answer through the route's response model, so it
compares what the API would return and honours `SCORES_IN_POSTGRES` as the server does. Runs in a
server pod (same env); an `HTTPException` is recorded as its status and detail.

- `record (--sample N | --observers FILE) --out DIR [--raw]`: per Observer its overview and stats,
  whitelist, network alerts, connections (every kind both orders, `verified_only`, each tier; up to
  3 pages by cursor), and for the first few followed users their overview and the shortest path
  from the Observer. Writes `observers.txt` (pass it as `--observers` to the next record) and
  `calls.jsonl`: per call the status, a hash of the normalised body (timestamp and request-id keys
  dropped, equal-Influence runs and `pubkeys` lists in a stable order) and the item count. `--raw`
  also keeps the bodies.
- `--sample N` (~20) reads the Scorecard and domain databases after the copy: House, the 3
  largest, the newest scored, 2 Free and 2 paid (scheduling assigned) near the median size, the
  rest spread over sizes.
- `diff A B [--reread]`: exit 1 on any differing or missing call (the whitelist is recorded but not
  compared: a run's snapshot before the flip, derived from stored scores after); saves into `B/mismatches/` the
  raw bodies both records kept, and with `--reread` a fresh read of each plus the three hashes, so
  a transient difference shows as `now == a`.

Held by `tests/test_compare_reads.py`.

### `rebuild_neo4j.py`

Rebuilds the Neo4j store without the per-Observer score properties, once Scorecards live in the
Scorecard database and the gate for it is met. Deleting the properties in place frees no file
space; exporting what is kept and importing it into a new store does. **The source database is
only read.** Stop every writer first (ingest, graph-streamer, the profile resolver, GrapeRank
runs): the export stops if the graph changes under it.

1. `inventory`: labels, relationship types and property keys, marked kept or not.
2. `export --out DIR`: `nodes.csv`, `relationships.csv` and their header files in the shape
   `neo4j-admin database import full` takes, plus `summary.json` (counts, the source's constraints
   and indexes, a sample of user nodes) and `import-command.txt`. Scores are filtered inside the
   database and never cross the wire.
3. The import: run the command in `import-command.txt` with `neo4j-admin` against an empty store,
   with `DIR` mounted at `/import`, then start Neo4j on that store.
4. `schema --dir DIR --apply`, with `NEO4J_DB_URL` pointing at the rebuilt database: recreates
   the constraints and indexes the source had.
5. `check --dir DIR`: exit 1 unless node counts per label set, relationship counts per type,
   property counts per key (users and relationships), every non-user node, the sampled users
   (property by property, and their degree per relationship type) and the schema all match, and
   no score property key exists.

What is kept is listed at the top of the script (`KEPT_LABELS`, `KEPT_RELATIONSHIPS`,
`KEPT_USER_KEYS`); a label, relationship type or user property outside those lists stops the
export until it is added or dropped on purpose. Relationships keep all their properties. An empty
array cannot be carried and is counted. Node ids change; nothing refers to them.

Fallback if a rebuild is not possible: remove the properties in place in id-range batches, as
`clean_trusted_reporters_props.py` does. Space is then reused but the store files do not shrink.

Held by `tests/test_rebuild_neo4j.py`; the whole procedure is rehearsed end to end outside this repo.

## When to add a new script

Add one if:
- The task is admin-only and tedious to repeat (e.g. minting tokens).
- You need a deterministic smoke test you'll re-run after a schema or service change.

Don't add one if it should be a proper test — put it in a `tests/` directory and make it `pytest`-runnable.

## Conventions

- Most scripts import `from app.core.config import settings` to pick up env, and **need `.env` populated**. The exceptions are deliberate: `emit_flash_webhook.py`, `check_flash_credentials.py` and the `search_*.sh` set are stdlib-only and app-free so they can run against a **deployed** host with no `.env` — they take everything from flags and environment variables instead. A stdlib-only script that re-derives app logic (the Flash HMAC) must be held to the real one by a test.
- A script on the Scorecard database calls `unbounded_statements()` (`app/core/scorecard_database.py`) before first use: no `statement_timeout`, no client timeout.
- Output is plain `print()` — these are CLI tools, not library code.
- Exit codes: 0 = ok, non-zero = something failed. The smoke tests assert and let `AssertionError` propagate.
