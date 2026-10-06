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
  — same endpoint, but can **sign** an NWT so the observer = signer. Observer
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

### `clean_trusted_reporters_props.py`

Strips leftover `trusted_reporters_<observer>` props from `NostrUser` nodes (the
reporter count is computed live, so they are inert). Observer keys come from
`db.propertyKeys()`. Dry-run by default (property + node counts); `--apply`
removes in node-id chunks of `--batch`, one transaction each; idempotent. Space is reused, but store
files only shrink after dump/reload. Held by
`tests/integration/test_clean_trusted_reporters_props_integration.py`.

## When to add a new script

Add one if:
- The task is admin-only and tedious to repeat (e.g. minting tokens).
- You need a deterministic smoke test you'll re-run after a schema or service change.

Don't add one if it should be a proper test — put it in a `tests/` directory and make it `pytest`-runnable.

## Conventions

- Most scripts import `from app.core.config import settings` to pick up env, and **need `.env` populated**. The exceptions are deliberate: `emit_flash_webhook.py`, `check_flash_credentials.py` and the `search_*.sh` set are stdlib-only and app-free so they can run against a **deployed** host with no `.env` — they take everything from flags and environment variables instead. A stdlib-only script that re-derives app logic (the Flash HMAC) must be held to the real one by a test.
- Output is plain `print()` — these are CLI tools, not library code.
- Exit codes: 0 = ok, non-zero = something failed. The smoke tests assert and let `AssertionError` propagate.
