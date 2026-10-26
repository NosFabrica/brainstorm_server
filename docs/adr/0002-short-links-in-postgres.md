# Short links live in Postgres, because eviction — not speed or durability — is the axis

A short share link (`/s/<code>` → pubkey + relay set) is a **record of truth**: once handed out, the
code is public and must resolve forever. It lives in PostgreSQL (`short_url`), reached through
`app/repos/short_url_repo.py`. Redis keeps only the per-IP rate-limit counter.

The first implementation kept the record in the shared Redis.

## Status

accepted

## Considered Options

- **Redis only (what shipped first)** — rejected on eviction semantics, not on speed or durability:
  - **Speed is not the axis.** Redis `GET` ~0.1–0.2ms; an indexed Postgres lookup ~0.1–0.3ms. Both
    vanish under the HTTP round trip — `/user/overview` is 0.78ms p50 end to end on Postgres.
  - **Durability is not the gap either.** The cluster Redis runs `appendonly yes` on a 5Gi PVC with a
    daily RDB backup (04:00 UTC, 5-day retention).
  - **Eviction is the gap.** `maxmemoryPolicy: allkeys-lru` (24gb prod / 10gb staging) evicts live
    keys under memory pressure regardless of TTL. The AOF faithfully records the eviction, and a
    restore only recovers the 04:00 snapshot. Everything else in that Redis is *derivable* — evict
    `followed_by:*` and it recomputes. A short link cannot be recomputed: evicted means a public URL
    404s forever, unrecoverably, with no signal that it happened.
- **Postgres with a Redis read cache in front** — rejected *for now*. At sub-millisecond it buys
  nothing measurable and costs an invalidation path. Add it only if a profiler asks.
- **Postgres alone (chosen).**

## Consequences

- **Expiry is gone, deliberately.** `shorturl_ttl_seconds` and its two `EX=` call sites are deleted.
  For a record of truth, expiry is not a durability lever but a way to permanently break links that
  are already distributed — re-shortening mints a *new* code, so the pasted URL stays dead. TTL with
  refresh-on-read would be the *correct* policy in front of Postgres, where a miss falls back instead
  of 404ing; it is wrong as the only copy.
- **The dangling-index guard goes too.** It existed only because content and index were two separate
  Redis keys. One row makes that skew impossible.
- **Creation stays unauthenticated**, and deferring auth is safe *because* of this change.
  Anonymous shortening is a product requirement, so this is not a deferral of convenience — any
  future revisit has to keep logged-out sharing working, e.g. by capping anonymous mints rather than
  blocking them. What changed is the blast radius: on Redis, runaway minting filled the shared
  instance and evicted the graph cache, presenting as a mystery app-wide slowdown. On Postgres the
  worst case is a table growing — visible, bounded, cheap to clean up. The per-IP limit (bypassable
  until the trusted-proxy fix) is the cheap half and is fixed. Revisit auth if a row-count alert fires.
- **Rate-limit counters stay in Redis.** They are exactly the recomputable, expiring data an LRU cache
  is for.
- **Codes are 8-char Crockford base32 (uppercase, no `I`/`L`/`O`/`U`)**, stored `varchar`, matched by
  exact string, folded case-insensitively on lookup. Length lives in one generation-time constant and
  is never encoded in a route, query or validation regex — so widening or narrowing it later is a
  one-constant change with no migration, and every existing code keeps resolving.
- **Dedup is by `(pubkey, relay set)`**, normalised order-, duplicate-, case- and
  trailing-slash-insensitively, so sharing the same profile twice returns the same code and creates no
  second row.

## Amendment (2026-10-22): a code points at an entity

Phase 1 shortened only profiles. Every shareable thing now gets a code, so the row changes shape.
Everything above about Postgres, eviction and code format still holds.

- **A code stands for an entity, `(kind, entity_key)`, `UNIQUE`.** Kinds are a closed set —
  `profile`, `event`, `address`, `hashtag`, `tag` — each with its own key rule, normalised before
  storing. Same thing → same code, whatever hints the sharer's link carried. Storing the full path
  was rejected: it dedups per URL string, not per entity. This replaces dedup by
  `(pubkey, relay set)`. The server does not check the entity exists; an unknown one resolves to
  the not-found page.
- **No stored relay hints by default.** Anything shared from inside Brainstorm came from the search
  relay, which holds every shareable kind, so our own pages find it without hints. The search relay
  is never a hint: it refuses anonymous reads, and even after NIP-42 AUTH a key with no kind-10040
  gets an empty answer to an exact-id read — useless to the outside clients hints exist for.
  Mandatory hints were rejected: more data for no reader.
- **Relays are stored only for entities loaded from foreign hints** (e.g. a pasted `nevent` with
  someone else's relays). **First mint wins**: a later mint never changes them.
- **DList addresses (kinds 39998/39999) with no stored relays get `wss://dcosl.brainstorm.world`**
  in the resolved link, where anonymous reads work. Added at resolve time, never stored.
- **No expiry, unchanged.** Rows are ~100 bytes; the reasoning above holds.
- **The old `{pubkey, relays}` contract is dropped, not shimmed.** Server, UI and og ship together.
  Existing rows migrated to `kind=profile`, `entity_key=pubkey`, relays and codes kept — prod's 13
  codes all still resolve; staging duplicates kept the oldest per pubkey.
