# app/routers/shorturl

A tiny URL shortener: maps a short code to a `{pubkey, relays}` payload.
Backed entirely by Redis — no Postgres, no Neo4j. Business logic lives in
[`app/services/shorturl_service.py`](../../services/shorturl_service.py); this
router is a thin HTTP wrapper.

URL prefix: `/shorturl` (registered in [`routers/router.py`](../router.py)).

## Endpoints

| Method | Path | Auth | Response | Notes |
|---|---|---|---|---|
| POST | `/shorturl` | none, **rate-limited 1 req/s/IP** | `CreateShortUrlResponse` (`data.shortCode`, `data.content`) | Body: `CreateShortUrlBody{pubkey, relays}`. Idempotent per `(pubkey, relay-set)`. |
| GET | `/shorturl/{short_code}` | none | `GetShortUrlResponse` (`data.pubkey`, `data.relays`) | 404 if unknown/expired. |

## Behaviour

- **Short code** = 12 random alphanumeric chars (`secrets`-based), claimed
  atomically via Redis `SET NX`.
- **Dedup / idempotency** — the same `(pubkey, relay-set)` always returns the
  same code. A relay set is order- and duplicate-insensitive and normalized
  (trimmed, lowercased, trailing slash stripped) before fingerprinting, so
  `[r2, r1/]` and `[r1, r2]` collapse to one code.
- **`[]` (empty relay list) is valid** and gets its own code. The only relay
  rules: each provided relay must be a well-formed `ws://`/`wss://` URL
  (format check only), and at most `MAX_RELAYS = 7` relays.
- **Expiry** is opt-in via `settings.shorturl_ttl_seconds` (env
  `SHORTURL_TTL_SECONDS`). `None` = never expire (default). When set, content
  and index keys carry the same TTL and auto-delete together — no sweep job.

## Rate limiting

The POST has a `Depends(rate_limit_create_short_url)` that calls the generic
[`validate_rate_limit`](../../utils/rate_limiting/rate_limiting.py) with
`key_prefix="shorturl_create"`, `limit=1`, `window_seconds=1`. Client IP is
taken from `X-Forwarded-For` (first hop) when present, else `request.client`.

## Redis layout

| Key | Type | Holds |
|---|---|---|
| `shorturl:content:<code>` | string (JSON) | `{"pubkey", "relays"}` — what GET resolves |
| `shorturl:fp:<pubkey>:<fingerprint>` | string | the short code — O(1) reverse index for dedup |
| `rate_limit:shorturl_create:<ip>` | string (counter) | per-IP fixed-window counter |

The fingerprint key replaced an earlier per-pubkey hash specifically so the
index expires *with* the content under TTL. A dangling index entry (content
gone, index lingering) is detected on create and regenerated.
