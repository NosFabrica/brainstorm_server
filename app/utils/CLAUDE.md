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
│   └── rate_limiting.py     # Redis-backed rate limiter + trusted-proxy client IP
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

Redis-backed fixed-window counters (`INCR` + `EXPIRE` on first hit). Used for
endpoints that need throttling beyond the per-user "frequent graperank request"
check in `user_service.py`.

- `RateLimitPolicy(key_prefix, limit, window_seconds)` — one named throttle.
  `GRAPERANK_POLICY` (`"graperank"`, 3 / 1800s) is shared by `POST
  /user/graperank` and `POST /user/followList`; `/shorturl` POST defines its own
  (`"shorturl_create"`, 1 / 1s) in its router.
- `validate_rate_limit(ip, policy)` — the only per-IP limiter. Counter key is
  `rate_limit:<key_prefix>:<ip>`.
- `validate_subscription_refresh_allowed(pubkey)` (12 / 60s) and
  `validate_flash_record_read_allowed(operator_pubkey)` (30 / 60s) — per-pubkey
  billing throttles. All limiters share `_enforce_window`.
- `resolve_client_ip(request)` — the caller's address, for limiter keys. **Reads
  the hop our own proxy wrote, not the first one.** The ingress *appends* to
  `X-Forwarded-For` rather than replacing it, so anything the client sends
  survives at the front of the chain; trusting `[0]` lets a caller rotate a
  forged header and bypass the limit entirely. `settings.trusted_proxy_hops`
  (default 1, env `TRUSTED_PROXY_HOPS`) is how far from the right our entry sits
  — raise it if a CDN or WAF is ever put in front.

  The fallback to the direct peer **logs a warning**, deliberately. uvicorn runs
  without a trusted `forwarded_allow_ips` (it defaults to `127.0.0.1`, and the
  ingress pod is not loopback), so `request.client.host` behind the ingress is
  the *ingress pod's* address — the same string for every caller. Falling back
  silently would throttle unrelated callers as one.

### The graperank counter key moved (issue 01)

It used to be `rate_limit:<request.client.host>` with no prefix. Because of the
uvicorn behaviour above, that resolved to the ingress pod address in production —
so it was **one global bucket of 3 requests / 30 min shared by every caller**,
not a per-IP limit. It is now `rate_limit:graperank:<real client ip>`.

Two consequences, both intended: live counters were abandoned once on deploy, and
the throttle changed from global to genuinely per-caller (a large capacity
increase). Splitting `/user/graperank` from `/user/followList`, which still share
the bucket, is issue 09.

## constants.py

Currently empty/near-empty (1 line). Reserve for truly app-wide constants
(envinronment-independent). Anything env-driven goes in `app/core/config.py`,
not here.

## Conventions

- Utils should be **pure** or near-pure. If a util needs DB access, it's a repo. If it needs HTTP, it's a service. The grey area (this folder) is for stateless / trivially-stateful helpers.
- No imports from `app/services/` or `app/routers/`. Utils sit at the bottom of the dependency graph.
- Test under `tests/` (see `tests/test_assistant_nip05.py` for a pure-helper example).
