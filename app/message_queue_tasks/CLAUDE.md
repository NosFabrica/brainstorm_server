# app/message_queue_tasks

Redis-driven async consumers that run as long-lived background tasks (spawned
in `app/api.py` lifespan). One module per queue / topic. The consumer functions
loop forever (`while True: await blpop(...)`) so they're started with
`asyncio.create_task` and cancelled in the lifespan teardown.

## Queues + their consumers

| Redis queue | Consumer (in `message_queue_consumer.py`) | Per-message handler | Side effects |
|---|---|---|---|
| `strfry:events` | `consume_strfry_plugin_messages()` | `process_strfry_event()` in `process_strfry_event.py` | Kind 3/10000/1984 → Neo4j relationship updates + Redis reverse-set caches. Kind 10040 → `DESIGNATES` edges + the forward `designates:` set. Kind 5 → recompute the author's report edges from the relay. Kind 39999 → persist tag elements / taggings to Postgres (Trusted Lists input). Other kinds (incl. 0) are ignored. |
| `nostr_results_message_queue` | `consume_nostr_upload_messages()` | `process_nostr_upload_message()` in `upload_nostr_events.py` | Sign + publish TA events to Nostr relays. |
| (other queues, see `message_queue_consumer.py`) | `consume_messages`, `consume_neo4j_write_messages`, `consume_job_started_messages` | … | … |

## Result queues (`result_queue.py`)

`results_message_queue`, `nostr_results_message_queue` and `write_neo4j_message_queue` carry an
envelope `{private_id, result_key}`; the worker stores the result once at `result_key` and names
the queues still needing it in `<result_key>:pending`. Each consumer `BLMOVE`s into
`<queue>:processing`, acks after the handler, then removes its name — the last one deletes the
payload (7d TTL only catches leaks). On start, `:processing` is requeued at the head, so a
consumer killed mid-handler retries; after `MAX_ATTEMPTS` the stage is marked `FAILURE`, as is a
missing payload. Handler exceptions are not retried. Inline `result` messages (failure results)
still work. **Assumes one consumer per queue** — recovery would steal a live replica's message.

## process_strfry_event.py

Dispatches incoming Nostr events by kind:

- **Kind 3** (contacts/follows) → upsert `FOLLOWS` relationships in Neo4j + maintain `followed_by:<pubkey>` Redis reverse-sets.
- **Kind 10000** (mute list) → same shape as kind 3 but with `MUTES` relationships and `muted_by:` sets.
- **Kind 1984** (reports) → `REPORTS` relationships + `reported_by:` sets. **Only user-level reports count**: per NIP-56 a note/media report carries an `e` tag, a user report is `p`-only.
- **Kind 10040** (designations) → `process_event_kind_10040`. Replaces the author's `DESIGNATES` edges (one write: drop the ones no longer listed, merge the rest) and their **forward** set `designates:<author>` (DEL + SADD in one MULTI). Forward, unlike the three above: a provider key is designated by many and designates nobody, so a reverse set would be a hub GrapeRank reads whole every run; the worker reads `designates:` for each user it reaches instead. Only public rows (`parse_designated_pubkeys`). Backfill: `scripts/backfill_designations.py`. See [ADR 0005](../../docs/adr/0005-designations-are-graph-edges.md).
- **Kind 5** (NIP-09 deletion) → `process_event_kind_5`. Reconciles the deleting author's `REPORTS` edges + `reported_by:` membership.
- **Kind 39999** (Decentralized-Lists item) → `process_event_kind_39999`. Tag elements and taggings share this kind and are told apart by their `z` tag (`app/services/tagging_parse.py`); anything matching neither concept is foreign traffic and is dropped. Persists to `nostr_tag_element` / `nostr_user_tagging` — the input set for Trusted Lists.

The reverse-set helper is `_update_reverse_sets`. Use it for any new "X → relationships → reverse-cache" pattern.

## upload_nostr_events.py

The longest module here. The main entry point is `process_nostr_upload_message(message)`. Big picture:

1. Read the inbound `GrapeRankResult` (a plain dict, see `app/models/CLAUDE.md`). Bail if no scorecards.
2. Resolve the observer's nsec via `get_or_create_brainstorm_observer_nsec_by_pubkey_on_db`.
3. Plan the run: TA inputs (above-cutoff scorecards) + the relay delete set for dropped pubkeys.
4. Sign and publish as one stream (`sign_publish_events` → `relay_publisher.publish_events`): TAs, then kind-5 deletions, signed locally as JSON and sent over raw websockets to `nostr_upload_ta_events_relay`. Every event must get a `true` OK — rejections and dropped connections are retried — or the run fails without advancing `last_published_pubkeys`.
5. Mark the brainstorm request as `SUCCESS` and persist the published-pubkey list — reached only when every event was OK'd. A full relay run resets the every-Nth backstop counter.

### Publish mode and deletes

- `relay_full_sync` (default `False` = delta) or the request row's `force_full_relay`: `False` publishes only _changed_ scores (`changedScorePubkeys`), `True` re-asserts every above-cutoff scorecard. Drift is repaired by the `full_sync_every_n_runs` backstop + admin `resync`, not by leaving it `True`. **Re-assertion only — never deletes.**
- The delete set is computed by `plan_publish` from a local diff (no relay read): `fell_off = previously_published − currently_above_cutoff`.
- The **sweep** (`relay_sweep_below_cutoff`, default off) adds every below-cutoff Observee to the delete set, reaping orphans the diff can't see. Backwards-compat drain: on to clear the legacy backlog, then off — it's mostly no-op deletes that each cost a tombstone. Not implied by full-sync, the backstop, or `resync`.

### Relay publishing (`relay_publisher.py`)

Raw websockets, not nostr-sdk. nostr-sdk's `send_event` does wait for the OK,
but one event per await, which was too slow; the publish had moved to
`relay.send_msg`, which only enqueues (no OK, drops on a full channel). Here
many events are in flight per connection and each OK is still tracked, and
there is no turning signed JSON back into `Event`s (costs more than signing). `PublishConfig` holds the knobs (4 connections, ≤2k
in flight per connection, ≤10k unresolved events in memory, 30s ack timeout,
retry/backoff caps). The per-run `TA publish timing` log carries `t_acked`,
`t_sign_cpu`, `n_acked`/`n_failed`/`n_retried`/`n_reconnects`/`n_rejected_*`,
`loop_lag_max_ms`/`loop_lag_p99_ms` and `rss_peak_mb` (`loop_probe.py`).

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
