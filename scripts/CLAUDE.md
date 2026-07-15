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

### Search testing helpers (`search_*.sh`, `search_open_ranking.py`)

Ad-hoc tools for poking the three search surfaces against a **deployed** host
(default: staging). The `.sh` ones are stdlib-only (curl/websocat/python3) and
need **no `.env`**; override the target via `BRAINSTORM_HTTP` / `BRAINSTORM_WS`.

- **`search_http.sh "<query>" [maxHits] [--all] [--tsv]`** — `GET /search/byText`
  (the default-observer path the UI uses logged-out). Prints rank order with
  `_relevance` + `_quality_score` so you can see *why* it ordered that way.
- **`search_nip50.sh "<search string>" [limit] [--tsv]`** — drives the NIP-50
  relay over a websocket. Put NIP-50 tokens in the search string to test them:
  `observer:<hex>` (rank POV), `sort:rank:desc|asc`, `filter:rank:gte:N`.
- **`search_compare.sh "<query>" [limit]`** — runs the HTTP + NIP-50 paths for
  the same query and prints them side by side, flagging divergence. Both share
  `app.core.vespa.search`, so a plain query matches; divergence means the NIP-50
  side resolved a different observer or a sort/filter profile. See
  `docs/search-trust-vs-exact-match.md`.
- **`search_open_ranking.sh "<query>" [limit] [--pov <hex>] [--algo <id>]`** —
  ORE-05 `POST /search/pubkeys`, **unsigned** (ORE-05 auth is optional and
  currently off). Stdlib-only. `--pov` only matters with a *personalized*
  algorithm, so it auto-selects `relevance-pov` (the default `relevance` is
  global and ignores pov per ORE-01).
- **`search_open_ranking.py --query <q> [--pov <hex>] [--algo <id>] [--sign|--nsec ...]`**
  — same endpoint, but can **sign** an NWT so the observer = signer. Observer
  priority: signer (when signed) → `--pov` (auto `relevance-pov`) → server
  default. Needs `requests` + `nostr-sdk` (poetry env), like
  `smoke_open_ranking.py`; no `.env` required.

### `trigger_graperank_all.py`

Bulk-re-runs GrapeRank for **every** observer in `brainstorm_nsec` — the backfill
lever for repopulating per-observer Vespa tensors (e.g. the new `follower_counts`,
docs/search-vs-tapestry.md §8/§9). GrapeRank is expensive per observer, so:

- `--rate N` enqueues/min, `--limit N` cap per run — tune to the worker's throughput.
- Resumable + observable via the `brainstorm_request` table (no fragile state):
  a campaign starts at time T (stored in `--state-file`); an observer is
  *triggered* if it has a `graperank` request since T and *completed* when that
  request hits `success`. Re-running skips already-triggered observers.
- `--status` (counts, no writes), `--dry-run` (preview). Run **inside a
  brainstorm-server pod** (needs `.env` + DB + the graperank worker consuming).

Note: the default observer alone covers anonymous/default search; this is for
populating ALL personalized perspectives at once. See §8.5/§9.

### `refeed_kind0_to_vespa.py`

Re-feeds every kind-0 profile from the internal strfry into Vespa via the SAME
ingest path the live consumer uses (`process_event_kind_0` → content/tags merge →
`upsert_profile`). The backfill for P1 (docs/search-vs-tapestry.md §8.4/§9):
populates the new `username` field and rebuilds the newly-indexed
`nip05`/`lud16`/`website` fields for existing docs. The transferer only handles
kinds 3/10000/1984, so this is the only kind-0 → Vespa re-feed path.

- **Requires the schema changes deployed first** (username + indexed fields) —
  else every `upsert_profile` fails on an unknown field.
- Walks kind-0 newest→oldest by `until` cursor, dedups by pubkey, writes the
  cursor to `--state-file` after each page → resumable.
- `--concurrency` (parallel Vespa writes), `--page`, `--limit`, `--status`,
  `--dry-run`. Run inside a brainstorm-server pod (needs `.env` + strfry + Vespa).

## When to add a new script

Add one if:
- The task is admin-only and tedious to repeat (e.g. minting tokens).
- You need a deterministic smoke test you'll re-run after a schema or service change.

Don't add one if it should be a proper test — put it in a `tests/` directory and make it `pytest`-runnable.

## Conventions

- All scripts import `from app.core.config import settings` to pick up env. **You need `.env` populated** to run any of them.
- Output is plain `print()` — these are CLI tools, not library code.
- Exit codes: 0 = ok, non-zero = something failed. The smoke tests assert and let `AssertionError` propagate.
