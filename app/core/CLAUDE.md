# app/core

Shared, dependency-free infrastructure modules. Everything else imports from here.

## Modules

| File | Purpose |
|---|---|
| [`config.py`](config.py) | Pydantic `Settings`. All env vars are declared here. `settings = Settings()` is the singleton other modules import. To add a setting: add the `Field(...)` here AND mirror in `../../env.example` AND any compose file that runs this service. |
| [`loggr.py`](loggr.py) | Logging setup. Always do `from app.core.loggr import loggr; logger = loggr.get_logger(__name__)`. Don't use `print` or stdlib `logging.getLogger` directly. |
| [`vespa.py`](vespa.py) | Vespa HTTP client + search helpers. See section below. |
| `admin_whitelist.py` / `billing_admin_whitelist.py` | Who may reach `/admin/*` and `/admin/billing/*` respectively. Separate lists on purpose — billing views carry payment data, and whoever answers payment questions should not inherit nsec key rotation. Billing falls back to the admin list when unset, so an existing deployment keeps working; once configured it is authoritative. Both parse via `admin_whitelist.parse_pubkey_list`. |
| [`flash.py`](flash.py) | Flash subscription reads (payments). Same shared-client shape as `vespa.py` — lazy module-level `_client`, `aclose()` in the lifespan — but the **inverse failure policy**: Vespa is a mirror whose failures are swallowed, while an unreadable Flash must raise, because returning "no subscription" for a socket timeout would revoke someone who is paying. `FlashUnavailable` is transient and retried; `FlashCredentialError` is not. A 404 is absence on the path lookups (`/subscriptions/{id}`, `/{id}/verify`) and a failure on the filtered list (`?ref=`). Also holds the plan read (`GET /services/{id}`), where a 404 is neither: it raises `FlashServiceMissing`, because our own configuration names a service Flash does not have. |
| [`flash_plan_cache.py`](flash_plan_cache.py) | Flash's plans, cached in Redis, because `/billing/plans` is public and unauthenticated. Two entries per service: a short-lived copy that keeps anonymous traffic off Flash's quota, and a **last-known-good copy with no expiry** served whenever Flash cannot be read — a TTL on that one would let an outage empty the pricing page, which is what it exists to prevent. Only "we could not ask" falls back; `FlashServiceMissing` passes through. |
| [`flash_settings_cache.py`](flash_settings_cache.py) | The account's acceptance methods, cached the same way and for the same reasons: each `amt_…` token from `GET /settings` against how it pays (`lightning`, `card`). Read on every signed-in billing page and every load of the admin roster, so a TTL keeps Flash out of the render path and a last-known-good copy keeps an outage from blanking a method that was right a minute ago. One difference from the plans: an unreadable Flash with nothing stored returns an empty map rather than raising — a token we cannot resolve is a payment method we do not show, which is the same answer an ambiguous plan already gives. |
| `database.py` | Async PostgreSQL `db_session()` context manager (asyncpg via SQLAlchemy). |
| `redis_db.py` | `redis_client` async singleton + queue helpers. |
| `sql_admin_panel.py` | SQLAdmin integration mounted by `app.api`. |
| `tier_thresholds.py` | The fixed tier bands + `classify_tier`. The read endpoints bucket subjects in Cypher (`user_repo._TIER_PREDICATES`); the GrapeRank result writer buckets in-memory scorecards it hasn't written to the graph yet, so it can't run that query and needs this second implementation. `tests/integration/test_tier_classifier_matches_cypher.py` asserts the two agree. `DEFAULT_VERIFIED_THRESHOLD` is a fallback only — the live verified line comes from the observer's saved preset. |

## vespa.py — the only Vespa client

This is the **only** module that should talk to Vespa. Everything else imports from it.

### Public surface

| Function | Use |
|---|---|
| `get_document(pubkey)` | Fetch a doc's fields by pubkey; `None` if 404. |
| `upsert_profile(pubkey, profile)` | Partial-update kind-0 profile fields (`PROFILE_FIELDS`). Creates the doc if absent. Missing fields are cleared to `""`. |
| `upsert_score(pubkey, observer, score, followers)` | Insert/replace the observer's cell in BOTH the `quality_scores` (rank) and `follower_counts` (verified-follower count) tensors. |
| `remove_score(pubkey, observer)` | Delete one tensor cell. 404s are silently ignored. |
| `batch_upsert_scores(upserts, removes, observer)` | Fan out many `upsert_score`/`remove_score` calls concurrently (bounded by `_BATCH_CONCURRENCY = 32`). Returns `(n_ok, n_failed)`. Individual exceptions are caught + logged, never propagate. |
| `search(query_text, user_pubkey, hits, include_zero_score_results)` | Multi-field search with the `text_relevance` (pure-text) rank profile. `user_pubkey` is the observer perspective (used by the trust-sorted rank_* profiles, not by the default). |
| `aclose()` | Close the shared httpx client. **Called from FastAPI lifespan shutdown** in `app/api.py`. |

### Shared client

A module-level `_client: httpx.AsyncClient` is created lazily on first use and re-used for every Vespa request. Connection pool: 200 max, 100 keep-alive. **Don't open ad-hoc `httpx.AsyncClient()` for Vespa calls elsewhere** — reuse this client.

### The YQL builders (`_word_group`, `_gram_clause`, `_about_gram_clause_for_word`, `_build_yql`)

Internal. They construct the OR-of-per-word-groups YQL used by `search()`. Port from the original `vespa_proj/api/main.py` prototype. The rank profile expects:
- one `@w{i}` query parameter per word (up to `MAX_QUERY_WORDS = 6`)
- optional `@wj` parameter for the joined CamelCase variant
- `ranking.features.query(w_gram)` / `query(w_about)` / `query(user_q)` — set in `search()`

If you change the rank profile in the Vespa app package (`brainstorm_one_click_deployment/vespa-app/schemas/doc.sd`), check whether the `ranking.features.query(...)` keys here still match.

### Failure policy

Vespa is a **search-side mirror**. Every score write the brainstorm-server does is also published to Nostr (the source of truth). If Vespa is down or rejects an update, the **caller's job still completes** — we log and move on. That's why `batch_upsert_scores` uses `return_exceptions=True` and `remove_score` swallows 404s.

If you ever need a Vespa write to be "must succeed," that's a design change — talk it through before flipping.
