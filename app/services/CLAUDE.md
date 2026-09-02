# app/services

Business logic. Sits between routers (HTTP) and repos (DB). Services orchestrate
the work — multiple repo calls, cross-store coordination, external HTTP, queue
publishing — and routers just thin-wrap them.

**Routers do not import repos directly. Services do.**

## Files

| File | LOC | Owns |
|---|---|---|
| `auth_service.py` | 24 | JWT mint / NIP-98 verify glue. The repo-less service — pure crypto + Redis. |
| `user_service.py` | 380 | Everything under `/user/*`: graph fetches, stats, connection pagination, assistant-profile publish, GrapeRank trigger throttling. The biggest service. |
| `brainstorm_request_service.py` | 157 | GrapeRank job lifecycle: create, status, result attach, error capture, throttle decisions. |
| `brainstorm_pubkey_service.py` | 54 | Create-or-get the per-user brainstorm-assistant pubkey (delegates to `brainstorm_nsec` repo + GrapeRank trigger). |
| `graperank_preset_service.py` | 109 | Builtin presets (DEFAULT/PERMISSIVE/RESTRICTIVE) get/set + history; bridges camelCase API ↔ snake_case columns. |
| `verified_cutoffs.py` | 95 | Resolves an observer's saved preset into the three per-relationship verified cutoffs (`follower`/`muter`/`reporter`) that `/stats`, `/overview` and `/connections` all compare Influence against. Inbound sections use their own cutoff; outbound sections and the tier `verified_line` use the follower cutoff. Strict `>`, no validity-floor clamp. |
| `assistant_profile_service.py` | 111 | Publish a kind-0 profile event for the assistant pubkey. Pure Nostr-side; no DB. `website` and the NIP-05 domain both come from `settings.frontend_url`; `nip05` is derived per-pubkey by `app/utils/assistant_nip05.py` and omitted when that URL has no hostname. |
| `nsec_encryption_service.py` | 210 | Background rotation of `BrainstormNsec.encrypted_nsec`: scan rows, decrypt-old/encrypt-new, write back. Idempotent and resumable. |
| `network_alerts_service.py` | 229 | `/networkAlerts`: builds the per-observer property keys and maps graph rows → panel payload. **Neo4j only** — the observer pubkey and their preset cutoffs arrive already resolved, from `routers/network_alerts/dependencies.py`. |
| `report_graph_service.py` | 120 | **Pure, no I/O.** The user-only report rules, shared by all three report paths (live kind-1984 ingest, the backfill script, the kind-5 recompute) so they cannot drift. Owns `extract_report_targets` (NIP-56 user-vs-note), the backfill's `build_desired_reported_by`/`diff_reported_by`, and kind-5's `deletion_may_target_reports`/`surviving_report_targets`/`diff_author_targets`. |
| `nip05_service.py` | 40 | NIP-05 document for `/.well-known/nostr.json`: the reserved `_` house identity from `settings.periodic_graperank_pubkey`, otherwise scan Assistant pubkeys and match the derived local-part. Hits also carry the recommended `relays` attribute (keyed by pubkey) from `nostr_upload_ta_events_relay_public_url`. Uncached by design. |
| `billing_service.py`            | 337 | Flash entitlement on the event path: the status decision table (`decide_entitlement`), the resolution truth table (`resolve_entitlement`), and `apply_entitlement`. Also the operator writes — blocking, plan mapping, and attributing or dismissing a signup that named nobody. Entitlement **is** the scheduling assignment; `user_subscription` only records why. |
| `billing_sync_service.py`       | 288 | The periodic half: replaying events we acknowledged then dropped, revoking what lapsed, re-reading Flash for what cannot be judged locally, and pruning old payloads. Split from the above because they change for different reasons; both still decide via `billing_service`, so the rules cannot drift. |
| `billing_visibility_service.py` | 209 | What nobody has settled: the divergence report an operator reads, and the accounting CSV. Read-only over the billing tables; first charges are priced from Flash's plan, since that event carries no amount. |
| `subscription_view_service.py`  | 298 | The UI-facing read side: one subscriber's view (`tier` from the scheduling assignment, Flash statuses translated on read) and the public plans list. Joins our mappings to Flash's plans through `core/flash_plan_cache`, so price, name, period, ordering, copy and both links are Flash's while the policy and the decision to sell are ours. The checkout link is Flash's `signupUrl` plus our `redirect_uri`; the manage link is the `portalUrl` recorded on the subscription. Nothing here spells a URL out of a base and an id. Read-only. |
| `leader_lock.py` | 30 | Redis leader lock, parameterized by key — generalizes the old `scheduler_lock` so the billing cron and the scheduler each hold their own. |
| `report_relay_service.py` | 90 | Reads an author's surviving kind-1984 back from the internal relay (REQ over websocket). Returns `None` for *unknown* vs `[]` for *no reports* — see `../message_queue_tasks/CLAUDE.md`. |

## Conventions

### Naming

Functions look like `<verb>_<noun>_<context>`. No `_on_db` suffix here (that's
a repo-only signal). Routers expose them under whatever HTTP shape they want.

### Errors

Services raise `HTTPException` directly with `ErrorResponseSchema(...)` in the
`detail`. Routers don't translate. That's why error handling lives close to the
domain logic.

Schema for errors: [`app/schemas/error_codes.py`](../schemas/error_codes.py)
and [`request_response_schemas.py`](../schemas/request_response_schemas.py).

### Transactions

Services open the session and pass it to repo functions:

```python
async with db_session() as db:
    obj = await create_brainstorm_request_on_db(db, ...)
```

If you need cross-repo atomicity, keep both calls inside one `db_session()`.
Repos don't commit; the context manager does.

**Declared exception: the billing subsystem commits explicitly**, in
`billing_service`, `billing_sync_service` and `flash_webhook_service`. Two
reasons, both structural rather than stylistic:

- The webhook receiver must have the event *durably recorded before it answers
  200*. Flash retries a delivery only a few times and then never replays it, so
  an event lost between the acknowledgement and a later commit is lost for good.
  Relying on dependency-teardown ordering for that is too subtle to be safe.
- The periodic jobs commit per item, so a crash forty rows into a batch keeps
  the thirty-nine already done. Every one of those writes is idempotent, so
  partial progress is strictly better than an all-or-nothing batch.

Don't extend this to new subsystems without the same kind of reason.

### Concurrency

Heavy fan-out → `asyncio.gather` with `return_exceptions=True` and explicit
log + count of failures. Don't propagate single-item failures to the whole
batch unless the domain says so.

`nsec_encryption_service.py` is the textbook example: it processes rows in
chunks, gathers, counts ok/fail, logs, and moves to the next chunk.

### State of `nsec_encryption_service`

Uses a module-level `_rotation_in_progress: bool` to enforce "one rotation at a
time" — the router returns 409 if it's already running. Suitable for a single
process; if/when you horizontally scale the server, push this flag into Redis.

## Per-service highlights

### `auth_service.py`

- `generate_nostr_auth_challenge(pubkey)` → stores challenge in Redis with TTL.
- `verify_nostr_auth_challenge(pubkey, signed_event)` → validates NIP-98-style signed event against the stored challenge, mints JWT via `app/utils/auth/auth_util.py`.

### `user_service.py`

- Owns the throttle: `_should_block_graperank_trigger(...)` checks `last_time_triggered_graperank` against `settings.block_frequent_graperank_requests_minutes`.
- Composes Neo4j calls (graph + influence) with Postgres (history). Avoid round-tripping per-section — there's a single `get_all_section_stats` for that.
- `get_user_overview` needs a `verified_line` (the observer's preset cutoff); `get_user_rank_and_counts` is the lean alternative for callers wanting only a rank + the raw counts, which takes no line and so needs no preset read. ORE-02 uses it.
- Manages the **observer key** for graph queries: defaults to `settings.periodic_graperank_pubkey`, else hardcoded fallback `be7bf5de068c1d842ed34a7c270507ec940f5ea51671cfd062a95e9d09420d0a` (same as the Vespa search service).
- Publishes the assistant kind-0 via `assistant_profile_service`.

### `brainstorm_request_service.py`

- `create_brainstorm_request(...)` resolves preset + custom params, snapshots them, creates the row in WAITING, returns the public-facing dict.
- `update_status_*` wrappers around the matching repo setters with the right error → schema mapping.
- Result attach lives here, not in the consumer, so retries are idempotent.

### `nsec_encryption_service.py`

- `verify_encrypted_nsec(...)` walks all rows, tries to decrypt each, returns `{ok, fail}`. Used by `/admin/nsec-encryption/verify`.
- `rotate_encryption(...)` (background task) does decrypt-with-old → re-encrypt-with-new for every row that's still on the old key. Idempotent: rows already on the new key are skipped.
- Both functions read keys from `settings.nsec_encryption_key` / `settings.nsec_encryption_key_previous`.

## Adding a new service

1. New `<topic>_service.py` here. Public functions are `async`.
2. Pull repo functions via `await ..._on_db(db, ...)`. Don't write raw SQL/Cypher in a service.
3. Raise `HTTPException(detail=ErrorResponseSchema(...))` for client-visible failures.
4. Router file calls `await my_service.do_thing(...)` and returns the wrapped response — see [../routers/CLAUDE.md](../routers/CLAUDE.md).
