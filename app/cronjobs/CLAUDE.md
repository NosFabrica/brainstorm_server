# app/cronjobs

Time-based background tasks. Each module exposes a coroutine that the
`app/api.py` lifespan launches via `asyncio.create_task` and cancels on
shutdown. Same long-lived-task pattern as `app/message_queue_tasks/`, but
driven by `asyncio.sleep` instead of Redis blpops.

## Jobs

### `fail_stale_ongoing_brainstorm_requests.py`

Sweeps stuck GrapeRank jobs. A request that's been ONGOING longer than
`settings.stale_brainstorm_request_threshold_minutes` is flipped to FAILURE
with a synthetic error so callers see a real terminal state instead of
"hung forever."

- Loop: `while True: await sweep(); await asyncio.sleep(settings.stale_sweep_interval_seconds)`.
- Delegates to `fail_stale_ongoing_brainstorm_requests_on_db` (repo). Logs the rowcount each cycle.
- **Safe to run on N replicas** — the UPDATE filters by `status=ONGOING AND updated_at < cutoff` so concurrent writers race-OK (worst case: one row written twice with the same terminal status).

### `periodic_graperank_trigger.py`

Triggers a periodic GrapeRank run for the *platform observer*
(`settings.periodic_graperank_pubkey`) at a fixed cadence.

- Reads `settings.periodic_graperank_interval_minutes`. If 0 or unset, the job is a no-op (the loop sleeps but never triggers — useful for dev).
- Calls into the same path as a user-triggered run: `brainstorm_request_service.create_brainstorm_request(...)` with `algorithm="graperank"` and the default preset.
- The result of the periodic run is what populates Vespa for search (only this observer's scores get mirrored — see [../message_queue_tasks/CLAUDE.md](../message_queue_tasks/CLAUDE.md)).
- **Don't run on multiple replicas** unless you also add a lock. Two processes will create two parallel requests every interval.

### `billing_sync.py`

Periodic billing reconciliation, off unless Flash is configured
(`settings.billing_sync_active` follows `flash_enabled` — the two being
separately switchable would let grants happen while revocations never do).

Four duties per cycle, in order of how far they can be trusted:

1. **Finish what we acknowledged and dropped** — a process killed between the
   200 and the entitlement write leaves a recorded event nothing acted on, and
   Flash will not send it again.
2. **Revoke what has provably lapsed** — locally decidable, no network.
3. **Re-read Flash for what is not** — `past_due` rows, ones still recorded
   `active` past their period end, and anything not asked about lately.
4. **Drop personal data from old events**, keeping the audit trail and the
   redelivery protection.

- **Safe on N replicas.** Correctness does not rest on the leader lock: replay
  claims each event through the database (`UPDATE … WHERE` decides, not whoever
  read first) and the other three are idempotent. The lock is there so N
  replicas don't all ask Flash the same questions.
- **The lock's TTL covers one cycle's work, not the gap between cycles.** At a
  six-hour interval nothing renews it — there is no second acquisition inside
  the window to renew from — so it is sized from the reconcile batch instead.
- **Background work yields to live deliveries.** Both duties take the
  subscriber's row lock with `SKIP LOCKED`; a webhook has ten seconds to
  acknowledge and this lock is held across a Flash read, so the deadline-free
  side is the one that steps aside.
- Its lifespan `finally` **awaits** the cancelled task before `flash_aclose()`:
  a cancelled reconcile can still be mid-GET, and closing the shared client
  under it would look like a Flash outage on every shutdown.

## Conventions

- Each cronjob is a self-contained `async def <name>_cronjob()` coroutine. No shared scheduler — `asyncio.sleep` in the loop is the rhythm.
- All sleep intervals come from `settings.*`, never hard-coded.
- Catch broad exceptions inside the loop so one bad cycle doesn't kill the task. Log + continue.
- Acquire a `db_session()` *inside* the loop body, not outside. A session that lives for the whole cron lifetime will hold a connection forever.

## Wiring a new cronjob

1. New `app/cronjobs/<name>.py` with `async def <name>_cronjob()`.
2. In `app/api.py` lifespan startup, add `<name>_task = asyncio.create_task(<name>_cronjob())`.
3. In the same lifespan's `finally:`, add `<name>_task.cancel()` + `await asyncio.gather(<name>_task, return_exceptions=True)`.
4. Add the cadence setting to `app/core/config.py` and `env.example`.
