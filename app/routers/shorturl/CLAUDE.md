# app/routers/shorturl

A tiny URL shortener: maps a short code to a `{pubkey, relays}` payload.
Backed by Postgres (`short_url`) — the record of truth, deliberately not Redis:
an evicted code would 404 a public URL permanently and cannot be recomputed.
Business logic lives in
[`app/services/shorturl_service.py`](../../services/shorturl_service.py); this
router is a thin HTTP wrapper.

URL prefix: `/shorturl` (registered in [`routers/router.py`](../router.py)).

## Endpoints

| Method | Path | Auth | Response | Notes |
|---|---|---|---|---|
| POST | `/shorturl` | none, **rate-limited 1 req/s/IP** | `CreateShortUrlResponse` (`data.shortCode`, `data.content`) | Body: `CreateShortUrlBody{pubkey, relays}`. Idempotent per `(pubkey, relay-set)`. Bad input is a **422** from the request schema. |
| GET | `/shorturl/{short_code}` | none | `GetShortUrlResponse` (`data.pubkey`, `data.relays`) | 404 if unknown/expired. |

## Validation

All of it lives in `CreateShortUrlBody`, so malformed input never reaches the
service and the framework answers 422 with a field-level body:

- **`pubkey`** — hex or npub in, **normalised to hex** before storage, matching
  `resolve_pubkey_or_400`. Anything else is rejected; a malformed pubkey is
  never stored.
- **`relays`** — at most 7 (`CreateShortUrlBody.MAX_RELAYS`), each a `ws://` or
  `wss://` URL with a host. `[]` is valid.

The response attribute is `short_code` in Python and serialises as `shortCode`
on the wire via `serialization_alias` — the wire format is fixed, the frontend
is built against it.

## Behaviour

- **Short code** = 8 chars of Crockford base32 (`secrets`-based): uppercase,
  never `I`/`L`/`O`/`U`. Stored uppercase; resolution uppercases and folds the
  confusables (`I`/`L` -> `1`, `O` -> `0`), so a code works whether it was
  scanned from the uppercase QR payload, copied lowercase, or retyped by hand.
  The length is referenced only by the generator — nothing may infer it, so it
  can change later without breaking codes already shared.
- **Dedup / idempotency** — the same `(pubkey, relay-set)` always returns the
  same code. A relay set is order- and duplicate-insensitive and normalized
  (trimmed, lowercased, trailing slash stripped) before fingerprinting, so
  `[r2, r1/]` and `[r1, r2]` collapse to one code.
- **`[]` (empty relay list) is valid** and gets its own code. The relay rules
  live in the request schema — see **Validation** above.

## Rate limiting

The POST has a `Depends(rate_limit_create_short_url)` that calls the generic
[`validate_rate_limit`](../../utils/rate_limiting/rate_limiting.py) with
`key_prefix="shorturl_create"`, `limit=1`, `window_seconds=1`. Client IP comes
from the shared `resolve_client_ip` helper, which reads the hop **our ingress
wrote** (`settings.trusted_proxy_hops` from the right of `X-Forwarded-For`),
not the first one —
the ingress appends rather than replaces, so a client-supplied leading entry is
attacker-controlled. This endpoint is unauthenticated, so that limit is its only
throttle.

## Storage

| Where | Holds |
|---|---|
| `short_url` (Postgres) | one row per code: `short_code` (unique), `pubkey`, `relays_fingerprint`, `relays` |
| `rate_limit:shorturl_create:<ip>` (Redis) | per-IP fixed-window counter — the only thing still in Redis |

Idempotency is a unique constraint on `(pubkey, relays_fingerprint)`, so a
concurrent double-mint loses the race in the database rather than orphaning a
row. There is no reverse-index key and no dangling-entry guard; both existed
only because the record used to be split across two Redis keys.
