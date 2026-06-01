# app/nostr_event_transferer

One-time backfill + ongoing incremental sync from external Nostr relays into our local strfry instance. This is the bootstrap path for the social graph data the GrapeRank algorithm needs; once strfry has the events, the strfry plugin pushes them onto the `strfry:events` Redis queue and they flow through [../message_queue_tasks/process_strfry_event.py](../message_queue_tasks/process_strfry_event.py).

## File

`nostr_event_transferer.py` — single module, ~330 LOC. Owns the whole transfer
pipeline.

## State machine

Per Nostr kind (3, 10000, 1984), state is stored in
[`brainstorm_nostr_relay_transfer`](../db_models/__init__.py) (UNIQUE on `kind`):

| field | meaning |
|---|---|
| `completed` | `False` until the full backfill is done; flips to `True` and stays. |
| `oldest` | resume cursor — epoch-seconds of the oldest event seen so far during backfill (and the "look back from here next time" pivot during incremental sync). |
| `events` | running count for telemetry. |
| `started_at` | when the current backfill kicked off. |

## Two modes

### Full backfill (`completed=False`)

- Subscribes to `[{"kinds": [K], "until": oldest_so_far}]` against every relay in `settings.relays_to_transfer_from`, in parallel.
- Streams events into strfry as they arrive. Maintains `oldest` continuously so that a crash + restart resumes near the last cursor.
- When all relays close the subscription (`EOSE` → graceful end), set `completed=True`.

### Incremental sync (`completed=True`)

- Periodic re-subscribe with `since=<some recent watermark>` to pick up new events.
- Doesn't touch `oldest`. Only `events` ticks up.

## Why this lives separately from `app/message_queue_tasks/`

`process_strfry_event.py` consumes events **after** strfry has them locally. The
transferer is **what gets them into strfry in the first place**. Separation of
concerns: queue consumers don't know about external relays; the transferer
doesn't know about Neo4j or the social-graph projection.

## Lifespan integration

Launched as a long-running `asyncio.create_task` in `app/api.py` startup, same
pattern as the cronjobs and queue consumers. Cancelled on shutdown.

## Conventions

- Uses [`app.repos.brainstorm_nostr_transferer`](../repos/brainstorm_nostr_transferer.py) for state reads/writes. **That repo commits internally** (the one inconsistency flagged in [../repos/CLAUDE.md](../repos/CLAUDE.md)).
- Relay connections via `websockets` async client. Per-relay tasks isolated — one relay misbehaving doesn't stall the others.
- Settings: `settings.relays_to_transfer_from` (list of WSS URLs), `settings.strfry_internal_relay_url`, and cadence settings.

## Common tasks

| I want to… | Do this |
|---|---|
| Add a new kind to backfill | Add the kind to whatever drives the transfer loop, add a `BrainstormNostrRelayTransfer` row implicitly via `upsert_nostr_transfer_status_on_db`, add the matching handler in `process_strfry_event.py`. |
| Add a relay | `settings.relays_to_transfer_from` list. No code change required. |
| Force-resync a kind | Delete the relevant row in `brainstorm_nostr_relay_transfer` (or set `completed=False`, `oldest=NULL`). The transferer will treat it as a fresh backfill on next loop. |
