# app/cronjobs

Time-based background tasks. Each module exposes a coroutine that the
`app/api.py` lifespan launches via `asyncio.create_task` and cancels on
shutdown. Same long-lived-task pattern as `app/message_queue_tasks/`, but
driven by `asyncio.sleep` instead of Redis blpops.

## Jobs

### `fail_stale_ongoing_brainstorm_requests.py`

Reaps stuck GrapeRank runs. A run still in the pipeline — calc `waiting`/`ongoing`, or
calc done with the TA publish `waiting`/`ongoing` — whose row hasn't changed for
`settings.stale_ongoing_brainstorm_request_threshold_hours` (7) is set to FAILURE, so a job
lost before or during the worker (e.g. a worker restarted by a deploy) can't block scheduler
admission. The Neo4j stage is not swept: it never blocks admission.

- Loop: sweep, then sleep `stale_ongoing_brainstorm_request_check_interval_minutes` (30).
- Delegates to `fail_stale_in_pipeline_brainstorm_requests_on_db` (repo). Logs the reaped run ids.
- A reaped run stays failed: the calc status is only written from `waiting`/`ongoing`, so a late
  worker can't set it back to `ongoing` or `success`.
- **Safe to run on N replicas** — the UPDATE filters on the in-pipeline condition and `updated_at`,
  so concurrent sweeps race-OK.

### `expire_support_diagnostics.py`

Drops the diagnostics snapshot off support tickets past `RETENTION`
(30 days), on `settings.support_diagnostics_sweep_interval_hours` (6).
**The cadence is a setting; the window is not** — how long personal data is
kept is a policy decision rather than a per-deployment knob. It guards the
policy and not the outcome, since an absurd cadence lengthens retention in
effect; that is drift to catch in review. **The only thing in
this database that expires** — everything else is kept, and `FlashWebhookEvent`
is kept on the stated grounds that it carries no personal data. This does not,
which is the whole reason it goes.

- Only the snapshot is cleared. The ticket, its messages and its events stay, and the thread still reads.
- **Dated from `created_at`, not `closed_at`** — the snapshot describes the moment of filing and is stale long before the window is out, and dating from closure would let a ticket nobody ever closes keep its snapshot forever. The cost is that a ticket still open on day 31 loses it while someone is working it.
- The UPDATE skips rows already cleared (`diagnostics IS NOT NULL`), or every sweep would rewrite every old row forever.
- **Safe to run on N replicas** — the filter is idempotent, so concurrent writers race-OK.

### `periodic_graperank_trigger.py`

Triggers a periodic GrapeRank run for the *platform observer*
(`settings.periodic_graperank_pubkey`) at a fixed cadence.

- Reads `settings.periodic_graperank_interval_minutes`. If 0 or unset, the job is a no-op (the loop sleeps but never triggers — useful for dev).
- Calls into the same path as a user-triggered run: `brainstorm_request_service.create_brainstorm_request(...)` with `algorithm="graperank"` and the default preset.
- Its published TAs are the house-observer scores the search relay serves to anonymous search.
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

- **Safe on N replicas.** Correctness does not rest on the leader lock: replay
  claims each event through the database (`UPDATE … WHERE` decides, not whoever
  read first) and the other two are idempotent. The lock is there so N
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
