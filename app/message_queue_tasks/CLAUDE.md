# app/message_queue_tasks

Redis-driven async consumers that run as long-lived background tasks (spawned
in `app/api.py` lifespan). One module per queue / topic. The consumer functions
loop forever (`while True: await blpop(...)`) so they're started with
`asyncio.create_task` and cancelled in the lifespan teardown.

## Queues + their consumers

| Redis queue | Consumer (in `message_queue_consumer.py`) | Per-message handler | Side effects |
|---|---|---|---|
| `strfry:events` | `consume_strfry_plugin_messages()` | `process_strfry_event()` in `process_strfry_event.py` | Kind 0 → Vespa profile upsert. Kind 3/10000/1984 → Neo4j relationship updates + Redis reverse-set caches. Kind 5 → recompute the author's report edges from the relay. Kind 39999 → persist tag elements / taggings to Postgres (Trusted Lists input). |
| `nostr_results_message_queue` | `consume_nostr_upload_messages()` | `process_nostr_upload_message()` in `upload_nostr_events.py` | Sign + publish TA events to Nostr relays, then mirror scores into Vespa via `batch_upsert_scores` (for any observer, keyed by the observer pubkey). |
| (other queues, see `message_queue_consumer.py`) | `consume_messages`, `consume_neo4j_write_messages`, `consume_job_started_messages` | … | … |

## process_strfry_event.py

Dispatches incoming Nostr events by kind:

- **Kind 0** (profile metadata) → JSON-parses `content`, calls `vespa.upsert_profile(pubkey, profile)`. Constants in the file:
  - `KIND_0_PROFILE_FIELDS` — imported from `app.core.vespa.PROFILE_FIELDS`. **If you add/remove a profile field, change it in `vespa.PROFILE_FIELDS`** — the constant here aliases it.
- **Kind 3** (contacts/follows) → upsert `FOLLOWS` relationships in Neo4j + maintain `followed_by:<pubkey>` Redis reverse-sets.
- **Kind 10000** (mute list) → same shape as kind 3 but with `MUTES` relationships and `muted_by:` sets.
- **Kind 1984** (reports) → `REPORTS` relationships + `reported_by:` sets. **Only user-level reports count**: per NIP-56 a note/media report carries an `e` tag, a user report is `p`-only.
- **Kind 5** (NIP-09 deletion) → `process_event_kind_5`. Reconciles the deleting author's `REPORTS` edges + `reported_by:` membership.
- **Kind 39999** (Decentralized-Lists item) → `process_event_kind_39999`. Tag elements and taggings share this kind and are told apart by their `z` tag (`app/services/tagging_parse.py`); anything matching neither concept is foreign traffic and is dropped. Persists to `nostr_tag_element` / `nostr_user_tagging` — the input set for Trusted Lists.

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

- `vespa_full_sync` / `relay_full_sync` (both default `False` = delta): `False` publishes only _changed_ scores (`changedScorePubkeys`), `True` re-asserts every above-cutoff scorecard each run. Drift is repaired by the `full_sync_every_n_runs` backstop + admin `resync`, not by leaving a sink `True`. **Re-assertion only — never deletes.** The k8s charts set these per env and override the code default.
- Below-cutoff scorecards are skipped for upserts.
- Delete sets are computed **per sink** by `plan_publish` from a local diff (no relay/Vespa read): `fell_off = previously_published − currently_above_cutoff`. Shared as one list when both sweep modes match.
- The **sweep** (`*_sweep_below_cutoff`, default off) adds every below-cutoff Observee to the delete set, reaping orphans the diff can't see. Backwards-compat drain: on to clear the legacy backlog, then off — it's mostly no-op deletes that each cost a tombstone/remove op. Not implied by full-sync, the backstop, or `resync`.
- All ops are fanned out concurrently — see `batch_upsert_scores` in `app/core/vespa.py`.

### Order of operations matters

Nostr publish happens **before** Vespa mirror. If you reorder these, Vespa could end up holding scores that were never persisted to Nostr — that breaks the "Nostr is source of truth" invariant.

## write_neo4j_results.py

Persists the per-observer scorecard properties listed in `PERSISTED_FIELDS`, as
`<field>_<observer_pubkey>`. The property names are built into a map in Python
and applied with `SET n += row.props` — never interpolated into the query text,
which would give every observer its own Neo4j plan-cache entry. Same rule the
read side follows via `n[$key]`; see [`../repos/CLAUDE.md`](../repos/CLAUDE.md).

## Adding a new consumer

1. Add an `async def consume_<topic>(): while True: msg = await ...; await handle(msg)` to `message_queue_consumer.py`.
2. Add `<topic>_task = asyncio.create_task(consume_<topic>())` in `app/api.py` lifespan.
3. Cancel it in the same lifespan's `finally:` block.
4. Per-message handler goes in a new module under this directory if it's nontrivial.
