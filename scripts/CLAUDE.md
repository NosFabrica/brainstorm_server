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

## When to add a new script

Add one if:
- The task is admin-only and tedious to repeat (e.g. minting tokens).
- You need a deterministic smoke test you'll re-run after a schema or service change.

Don't add one if it should be a proper test — put it in a `tests/` directory and make it `pytest`-runnable.

## Conventions

- All scripts import `from app.core.config import settings` to pick up env. **You need `.env` populated** to run any of them.
- Output is plain `print()` — these are CLI tools, not library code.
- Exit codes: 0 = ok, non-zero = something failed. The smoke tests assert and let `AssertionError` propagate.
