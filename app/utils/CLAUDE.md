# app/utils

Cross-cutting helpers that don't belong to any single domain. Keep this thin —
if a util is only used by one module, it belongs next to that module.

## Layout

```
app/utils/
├── api_validators.py        # FastAPI dependencies: verify_token (JWT or NIP-98)
├── assistant_nip05.py       # Deterministic Assistant NIP-05 derivation (pure)
├── auth/
│   ├── auth_util.py         # JWT mint/verify + password gen
│   ├── auth_models.py       # JWTData dataclass / TypedDict
│   └── nip98.py             # NIP-98 signed-event validation
├── bip39_english.txt        # Vendored BIP-39 wordlist used by assistant_nip05
├── encryption.py            # Fernet symmetric encryption for nsec storage
├── neo4j_values.py          # safe_float / safe_int — inf/nan coercion on graph reads
├── nostr.py                 # Tiny Nostr helpers (constants, format conversions)
├── rate_limiting/
│   └── rate_limiting.py     # In-memory / Redis-backed rate limiter
└── constants.py             # Truly app-wide constants
```

## api_validators.py — the auth dependency

`verify_token(request: Request) -> None` is the single FastAPI dependency that
all authed routers use. It populates `request.state.jwt_data: JWTData` so
handlers can read the caller's pubkey via `request.state.jwt_data.nostr_pubkey`.

Accepts either:

1. **JWT bearer** — `Authorization: Bearer <token>` or legacy `access_token` header.
2. **NIP-98** — `Authorization: Nostr <base64-event>`. Delegates to `auth/nip98.py` for signature + tag validation.

On failure it raises `HTTPException(401, detail=ErrorResponseSchema(...))`.

There's also a `verify_admin_access` dependency defined in
[`app/routers/admin/router.py`](../routers/admin/router.py) (not here — it's
admin-specific). It chains after `verify_token` and checks
`get_whitelisted_pubkeys()`.

## auth/

- **`auth_util.py`** — `mint_jwt(...)`, `verify_jwt(...)`, `generate_secure_password()` (used as default for `BrainstormRequest.password`).
- **`auth_models.py`** — `JWTData` (pubkey, expiry, optional `is_admin`).
- **`nip98.py`** — validates a NIP-98 signed event:
  - method/URL tags match the inbound request
  - signature verifies against the claimed pubkey
  - event is fresh (created_at within tolerance)
  - returns the pubkey on success, raises on failure

## encryption.py

Fernet wrapper used by [`app/services/nsec_encryption_service.py`](../services/nsec_encryption_service.py) and the `brainstorm_nsec` repo for transparent on-read decryption.

- `encrypt(plaintext: str, key: bytes) -> str` (base64-encoded Fernet token).
- `decrypt(token: str, key: bytes) -> str`.
- Two keys live in settings: `settings.nsec_encryption_key` (current) and `settings.nsec_encryption_key_previous` (for rotation). Decrypt path tries current then previous.

If you change anything here you **must** run the rotation service after deploy
or you'll have rows that can't be decrypted.

## nostr.py

Tiny Nostr helpers: `generate_random_nsec`, and `resolve_pubkey_or_400` (hex or npub in, canonical hex out, 400 on anything else — shared by `/networkAlerts` and `/shortestPath`). Treat it as a place to drop small helpers; if it grows past ~50 LOC, split by topic.

## rate_limiting/

Simple windowed counter (Redis-backed when running with a Redis URL configured,
in-memory fallback otherwise). Used for endpoints that need throttling beyond
the per-user "frequent graperank request" check in `user_service.py`.

## constants.py

Currently empty/near-empty (1 line). Reserve for truly app-wide constants
(envinronment-independent). Anything env-driven goes in `app/core/config.py`,
not here.

## Conventions

- Utils should be **pure** or near-pure. If a util needs DB access, it's a repo. If it needs HTTP, it's a service. The grey area (this folder) is for stateless / trivially-stateful helpers.
- No imports from `app/services/` or `app/routers/`. Utils sit at the bottom of the dependency graph.
- Test under `tests/` (see `tests/test_assistant_nip05.py` for a pure-helper example).
