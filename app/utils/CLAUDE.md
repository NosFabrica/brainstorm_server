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
├── shorturl_entities.py     # Short-link entity kinds: key normalisation + on-site path (pure)
├── webauthn.py              # WebAuthn get() assertion verification for the passkey vault (pure)
├── rate_limiting/
│   └── rate_limiting.py     # Redis-backed rate limiter + trusted-proxy client IP
├── datetimes.py             # utc_now() for the naive timestamp columns
├── etags.py                 # etag_digest() + the 304 helpers
└── constants.py             # Truly app-wide constants
```

## api_validators.py — the auth dependency

`verify_token(request: Request) -> None` is the single FastAPI dependency that
all authed routers use. It populates `request.state.jwt_data: JWTData` so
handlers can read the caller's pubkey via `request.state.jwt_data.nostr_pubkey`.

Accepts either:

1. **JWT bearer** — `Authorization: Bearer <token>` or legacy `access_token` header.
2. **NIP-98** — `Authorization: Nostr <base64-event>`. Delegates to `auth/nip98.py` for signature + tag validation.

A bearer token whose `session_version` is behind the user's (`repos/session_version_repo.py`, Redis-cached for 60s, written through on a bump) is a 401 "Session revoked"; tokens without the claim count as version 0. NIP-98 is never version-checked: it's signed per request.

On failure it raises `HTTPException(401, detail="...")` — `detail` is a plain string throughout this repo, see [`app/services/CLAUDE.md`](../services/CLAUDE.md) → Errors.

There's also a `verify_admin_access` dependency defined in
[`app/routers/admin/router.py`](../routers/admin/router.py) (not here — it's
admin-specific). It chains after `verify_token` and checks
`get_whitelisted_pubkeys()`.

## auth/

- **`auth_util.py`** — `create_jwt_token(...)` mints session tokens with standard `sub`/`iat`/`exp` (+ `is_admin` for UI display, `session_version` for revocation); `decrypt_jwt_token(...)` is the one session-token decode path (library-enforced expiry, `None` on any invalid token). SQLAdmin panel: `sql_admin_create_jwt_token` / `sql_admin_verify_jwt_token`. Also `generate_secure_password()` (used as default for `BrainstormRequest.password`), and `warn_if_session_secret_short()` — the startup warning (never a refusal) for an `AUTH_SECRET_KEY` under 32 bytes.
- **`auth_models.py`** — `JWTData` (pubkey, optional `is_admin`, `session_version`).
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

Tiny Nostr helpers: `generate_random_nsec`, the one tag reader (`tag_values` / `first_tag_value` over raw JSON tags, or `event_tags(event)` for a parsed `Event`), and `resolve_pubkey_or_400` (hex or npub in, canonical hex out, 400 on anything else — shared by `/networkAlerts` and `/shortestPath`). Treat it as a place to drop small helpers; if it grows past ~50 LOC, split by topic.

## rate_limiting/

Redis-backed fixed-window counters (`INCR` + `EXPIRE` on first hit). Used for
endpoints that need throttling beyond the per-user "frequent graperank request"
check in `user_service.py`.

- `RateLimitPolicy(key_prefix, limit, window_seconds)` — one named throttle.
  `GRAPERANK_POLICY` (`"graperank"`, 3 / 1800s) is shared by `POST
  /user/graperank` and `POST /user/followList`; `/shorturl` POST defines its own
  (`"shorturl_create"`, 1 / 1s) in its router.
- `validate_rate_limit(ip, policy)` — the only per-IP limiter. Counter key is
  `rate_limit:<key_prefix>:<client_bucket(ip)>`; `client_bucket` folds IPv6 to
  its /64 (one host normally holds the whole prefix). Every 429 carries
  `Retry-After` (the window's remaining TTL) and logs a warning naming the bucket.
- Login (`/authChallenge`): `rate_limit_login_challenge` / `rate_limit_login_verify`
  (per IP per minute) and `rate_limit_login_new_observer` (per IP per hour, only
  for verifies that would create an Observer row), all built by
  `rate_limit_per_ip(key_prefix, limit, window)`. Limits are settings
  (`LOGIN_*_RATE_LIMIT_*`), read per request; never per pubkey, or an attacker
  could spend a victim's bucket. The passkey vault's public half has the same
  shape: `rate_limit_passkey_challenge` / `rate_limit_passkey_open`
  (`PASSKEY_*_RATE_LIMIT_PER_MINUTE`).
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

  The fallback to the direct peer **logs a warning**, deliberately. If uvicorn's
  `FORWARDED_ALLOW_IPS` doesn't cover the ingress (the chart sets
  `server.forwardedAllowIps`), `request.client.host` is the *ingress pod's*
  address — the same string for every caller. Falling back silently would
  throttle unrelated callers as one.

### The graperank counter key moved

It used to be `rate_limit:<request.client.host>` with no prefix. Because of the
uvicorn behaviour above, that resolved to the ingress pod address in production —
so it was **one global bucket of 3 requests / 30 min shared by every caller**,
not a per-IP limit. It is now `rate_limit:graperank:<real client ip>`.

Two consequences, both intended: live counters were abandoned once on deploy, and
the throttle changed from global to genuinely per-caller (a large capacity
increase). `/user/graperank` and `/user/followList` still share the bucket.

Keyed by IP for the anonymous paths, by **pubkey** where the caller is
authenticated and the cost is per-account: the billing refresh poll, the Flash
record read, and support replies — the open-ticket cap bounds how many threads
exist, nothing else bounds how much is written into them.

## etags.py

`etag_digest(*parts)` — a validator over everything that decides what a response
would contain. Every part that varies the body must be passed, or a client
holding the tag for one view is told a different view is unchanged. `not_modified(etag)` and `tag_response(response, etag)` carry the 304 and the
headers, so the cache policy is written once. Used by both support listings;
`_whitelist_etag` in `routers/router.py` predates these and duplicates the
digest — worth migrating when someone is next in that file.

Nothing here is called *weak*: RFC 7232 reserves that for a `W/`-prefixed
validator, and these are plain quoted digests.

## datetimes.py

`utc_now()` — now in UTC, naive, which is what the naive timestamp columns hold.
Use it for any Python-side write into one. Bare `datetime.now()` is the app
host's local clock and skews against the columns the database fills with
`now()`, which can order a `closed_at` before the `created_at` beside it.

`as_naive_utc(dt)` — normalizes a DB value that may come back tz-aware (prod
timestamptz drift) to naive UTC before comparing against `utc_now()`.

## constants.py

Currently empty/near-empty (1 line). Reserve for truly app-wide constants
(envinronment-independent). Anything env-driven goes in `app/core/config.py`,
not here.

## Conventions

- Utils should be **pure** or near-pure. If a util needs DB access, it's a repo. If it needs HTTP, it's a service. The grey area (this folder) is for stateless / trivially-stateful helpers.
- No imports from `app/services/` or `app/routers/`. Utils sit at the bottom of the dependency graph.
- Test under `tests/` (see `tests/test_assistant_nip05.py` for a pure-helper example).
