# app/routers/shorturl

A tiny URL shortener: maps a short code to an entity `(kind, key)` plus optional
stored relays, and answers the full on-site path for it. Backed by Postgres
(`short_url`); why not Redis:
[ADR 0002](../../../docs/adr/0002-short-links-in-postgres.md).
Business logic lives in
[`app/services/shorturl_service.py`](../../services/shorturl_service.py); key
rules and paths in
[`app/utils/shorturl_entities.py`](../../utils/shorturl_entities.py). This router is a
thin HTTP wrapper.

URL prefix: `/shorturl` (registered in [`routers/router.py`](../router.py)).

## Endpoints

| Method | Path | Auth | Response | Notes |
|---|---|---|---|---|
| POST | `/shorturl` | none, **rate-limited 1 req/s/IP** | `CreateShortUrlResponse` (`data.shortCode`, `kind`, `key`, `relays`, `path`) | Body: `CreateShortUrlBody{kind, key, relays?}`. Idempotent per `(kind, key)`. Bad input is a **422** from the request schema. |
| GET | `/shorturl/{short_code}` | none | `GetShortUrlResponse` (`data.kind`, `key`, `relays`, `path`) | 404 if unknown; 422 if not `[A-Za-z0-9]{6,32}`. Codes never expire. |

## Kinds

| kind | key (stored form) | path |
|---|---|---|
| `profile` | 64-hex pubkey (npub accepted) | `/p/<npub>`, `/p/<nprofile>` with stored relays |
| `event` | 64-hex event id | `/e/<nevent>` (stored relays, no author) |
| `address` | `<kind>:<64-hex pubkey>:<d>`, split on the first two colons; `d` may be empty | `/e/<naddr>` (stored relays) |
| `hashtag` | trimmed, lowercased, one leading `#` dropped; 1–64 chars, no `/` or whitespace | `/t/<encodeURIComponent(word)>` |
| `tag` | `<64-hex author>:<slug>`, split on the first colon | `/tags/<author npub>/<encodeURIComponent(slug)>` |

Hex is lowercased. An `address` of kind 39998/39999 with no stored relays gets
`wss://dcosl.brainstorm.world` in its naddr (path only, never stored). The server
does not check the entity exists.

## Validation

All of it lives in `CreateShortUrlBody`, so malformed input never reaches the
service and the framework answers 422 with a field-level body:

- **`kind`** — closed set above.
- **`key`** — normalised to its stored form per kind; a malformed key is never stored.
- **`relays`** — optional, at most 7 (`CreateShortUrlBody.MAX_RELAYS`), each a
  `ws://` or `wss://` URL with a host that `nostr_sdk` can put into a NIP-19 entity.
  `wss://search.brainstorm.world` is silently dropped (never a hint), and skipped
  when building a path from a legacy row that stored it.

The response attribute is `short_code` in Python and serialises as `shortCode`
on the wire via `serialization_alias`; the other keys are single words.

## Behaviour

- **Short code** = 8 chars of Crockford base32 (`secrets`-based): uppercase,
  never `I`/`L`/`O`/`U`. Stored uppercase; resolution uppercases and folds the
  confusables (`I`/`L` -> `1`, `O` -> `0`), so a code works whether it was
  scanned from the uppercase QR payload, copied lowercase, or retyped by hand.
  The length is referenced only by the generator — nothing may infer it, so it
  can change later without breaking codes already shared.
- **Dedup / idempotency** — the same `(kind, key)` always returns the same code,
  whatever relays the caller sent. **First mint wins**: a later mint never
  changes the stored relays, and its response carries the stored ones.

## Rate limiting

The POST has a `Depends(rate_limit_create_short_url)`, built by the generic
[`rate_limit`](../../utils/rate_limiting/rate_limiting.py) factory with
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
| `short_url` (Postgres) | one row per code: `short_code` (unique), `kind`, `entity_key`, `relays` |
| `rate_limit:shorturl_create:<ip>` (Redis) | per-IP fixed-window counter — the only thing still in Redis |

Idempotency is a unique constraint on `(kind, entity_key)`, so a
concurrent double-mint loses the race in the database rather than orphaning a
row. There is no reverse-index key and no dangling-entry guard; both existed
only because the record used to be split across two Redis keys.
