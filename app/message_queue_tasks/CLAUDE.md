# app/message_queue_tasks

Redis-driven async consumers that run as long-lived background tasks (spawned
in `app/api.py` lifespan). One module per queue / topic. The consumer functions
loop forever (`while True: await blpop(...)`) so they're started with
`asyncio.create_task` and cancelled in the lifespan teardown.

## Queues + their consumers

| Redis queue | Consumer (in `message_queue_consumer.py`) | Per-message handler | Side effects |
|---|---|---|---|
| `strfry:events` | `consume_strfry_plugin_messages()` | `process_strfry_event()` in `process_strfry_event.py` | Kind 0 → Vespa profile upsert. Kind 3/10000/1984 → Neo4j relationship updates + Redis reverse-set caches. |
| `nostr_results_message_queue` | `consume_nostr_upload_messages()` | `process_nostr_upload_message()` in `upload_nostr_events.py` | Sign + publish TA events to Nostr relays, then mirror scores into Vespa via `batch_upsert_scores` (for any observer, keyed by the observer pubkey). |
| (other queues, see `message_queue_consumer.py`) | `consume_messages`, `consume_neo4j_write_messages`, `consume_job_started_messages` | … | … |

## process_strfry_event.py

Dispatches incoming Nostr events by kind:

- **Kind 0** (profile metadata) → JSON-parses `content`, calls `vespa.upsert_profile(pubkey, profile)`. Constants in the file:
  - `KIND_0_PROFILE_FIELDS` — imported from `app.core.vespa.PROFILE_FIELDS`. **If you add/remove a profile field, change it in `vespa.PROFILE_FIELDS`** — the constant here aliases it.
- **Kind 3** (contacts/follows) → upsert `FOLLOWS` relationships in Neo4j + maintain `followed_by:<pubkey>` Redis reverse-sets.
- **Kind 10000** (mute list) → same shape as kind 3 but with `MUTES` relationships and `muted_by:` sets.
- **Kind 1984** (reports) → `REPORTS` relationships + `reported_by:` sets.

The reverse-set helper is `_update_reverse_sets`. Use it for any new "X → relationships → reverse-cache" pattern.

## upload_nostr_events.py

The longest module here. The main entry point is `process_nostr_upload_message(message)`. Big picture:

1. Validate the inbound `GrapeRankResult`. Bail if no scorecards.
2. Resolve the observer's nsec via `get_or_create_brainstorm_observer_nsec_by_pubkey_on_db`.
3. Build the Nostr events to publish: TA assertions (above-cutoff scorecards) + deletion events for dropped pubkeys.
4. Publish all events to the configured relays (best-effort per relay).
5. Mirror scores to Vespa via `upsert_scores_to_vespa(...)` → `batch_upsert_scores`, keyed by this observer's pubkey in the `quality_scores` tensor (runs for every observer, not just `settings.periodic_graperank_pubkey`). Vespa failures are logged but don't fail the request.
6. Mark the brainstorm request as `SUCCESS` and persist the published-pubkey list.

### `upsert_scores_to_vespa` — the score-mirror function

- Two per-sink settings control "full re-assert vs incremental" (both `settings`, default `True`): `vespa_full_sync` (this Vespa mirror) and `relay_full_sync` (the Nostr relay — TA republish + kind-5 deletes). `True` pushes every above-cutoff scorecard each run; `False` only *changed* scores (`grape_rank_result.changedScorePubkeys`). Flip a sink `False` for steady state; run it `True` periodically to reconcile that sink's drift.
- Below-cutoff scorecards are skipped for upserts.
- Delete sets are computed **per sink**: `relay_pubkeys_to_delete` / `vespa_pubkeys_to_delete` = (all below-cutoff when that sink's full-sync is on, else `droppedBelowCutoffPubkeys`) **plus** previously-published pubkeys no longer in the scorecards (always removed from both). The two lists are shared when both modes match.
- All ops are fanned out concurrently — see `batch_upsert_scores` in `app/core/vespa.py`.

### Order of operations matters

Nostr publish happens **before** Vespa mirror. If you reorder these, Vespa could end up holding scores that were never persisted to Nostr — that breaks the "Nostr is source of truth" invariant.

## Adding a new consumer

1. Add an `async def consume_<topic>(): while True: msg = await ...; await handle(msg)` to `message_queue_consumer.py`.
2. Add `<topic>_task = asyncio.create_task(consume_<topic>())` in `app/api.py` lifespan.
3. Cancel it in the same lifespan's `finally:` block.
4. Per-message handler goes in a new module under this directory if it's nontrivial.
