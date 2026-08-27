# Pending checkouts, and the ones nobody finishes

Why a subscription row exists before anyone has been granted anything, what happens to it when the
payment never confirms, and why the reconcile sweep eventually stops asking about it.

Source of truth for Flash's own behaviour is `docs/PAYMENTS.md` in the **brainstorm workspace root**
(alongside this repo, not inside it) — the document Flash gave us. `docs.paywithflash.com` documents an
older product and is actively misleading; do not answer questions from it.

## What Flash does

A checkout redirects back with `status=active`, `trial` or `pending`. `pending` means the payment is real
but still being confirmed — some Lightning wallets or their relays answer slowly, so Flash keeps verifying
in the background. Flash's doc:

> Pending checkouts resolve within about 30 minutes: they either activate (you receive the webhook) or
> **are discarded** if the payment never confirms, after which the user can simply try again.

That discard is the thing to design around. A subscription we recorded as `pending` can simply cease to
exist on Flash's side, and every later lookup answers "no such subscription".

**The documented timing is not what staging does.** On 2026-08-26/27 a pending checkout was still
answering `pending` 14 hours after it was created, and had vanished 6 hours after that — against a
documented ~30 minutes and a ~10-minute invoice lifetime. Do not build a test around the 30 minutes.

## The flow

```mermaid
flowchart TD
    A["Subscriber completes checkout, ref = pubkey"] --> B{"Redirect status"}
    B -->|"active or trial"| C["POST /user/subscription/refresh"]
    B -->|"pending"| D["POST /user/subscription/refresh"]

    C --> E["Read Flash by ref"]
    E --> F["Upsert row, grant scheduling policy"]

    D --> G["Read Flash by ref, answer is pending"]
    G --> H["Upsert row, granted_scheduling_id stays NULL"]
    H --> I{"Does the payment confirm?"}

    I -->|"yes"| J["Webhook subscription.activated"]
    J --> K["Re-read Flash by subscriptionId"]
    K --> F

    I -->|"no"| L["Flash discards the pending checkout"]
    L --> M["Reconcile sweep reads by ref, gets nothing"]
    M --> N["last_sync_error = unknown_subscription, sync_error_since stamped once"]
    N --> O{"Failing for longer than the abandon window?"}
    O -->|"no"| M
    O -->|"yes"| P["Row leaves the sweep. Never deleted, still in the divergence report"]
```

Two asymmetries worth noticing. The webhook path looks Flash up by `subscriptionId`, taken from the
recorded event; every other path — the refresh endpoint, the admin resync, the sweep — looks up by `ref`,
because a subscription id in a request body would be a claim to someone else's payment. And no path ever
grants from the event body: each one re-reads Flash's API first, so concurrent handlers converge.

## Why the row is written before anything is granted

It would be tempting to record nothing until `subscription.activated` arrives. Two things break if we do.

**A lost activation would be unrecoverable.** Flash retries a failed delivery a few times and then never
replays it. The reconcile sweep is the only path that recovers one, and it reads `user_subscription` — so
a subscriber with no row is invisible to it. They would have paid, lost the delivery, and silently never
been granted, until they happened to hit refresh themselves.

**The user would be told they had not paid.** `subscription_view_service` maps `pending` to a `pending`
UI status, and *no row at all* to `none` — "you have no subscription", shown to someone who just paid.
Flash's doc asks for a confirming state on `status=pending`.

So the row is the handle that makes both work, and its cost is that a checkout nobody completed leaves one
behind. That is a reason to stop *sweeping* it, never to stop *recording* it.

Not to be confused with the pending-checkout subsystem the PRD deletes — email correlation, a 30-minute
matching window, an admin queue for unmatched payments. All of that was replaced by `ref`. This row is
just the ordinary subscription record carrying Flash's status verbatim.

## The row's life

```mermaid
stateDiagram-v2
    state "pending, nothing granted" as Pending
    state "active, policy granted" as Active
    state "pending, unknown to Flash" as Unknown
    state "pending, out of the sweep" as Abandoned

    [*] --> Pending: first read after checkout
    Pending --> Active: activated webhook, or a sweep that finds it active
    Pending --> Unknown: Flash discarded the checkout
    Unknown --> Active: a fresh checkout activates
    Unknown --> Abandoned: same error for longer than the window
    Abandoned --> Active: a fresh checkout activates
    Active --> [*]: expired or canceled, policy revoked
```

`Abandoned` is not a status and not a deletion. It is one named condition — `AbandonRule` in
`user_subscription_repo` — negated by the sweep and asserted by the report. The row keeps its `pending`
status and its `unknown_subscription` error, stays in the admin subscriptions list, and simply stops
costing a Flash call every cycle. Nothing in the system deletes a `user_subscription` row.

Deliberately *not* a second value in `last_sync_error`. That column records what Flash said; "abandoned"
is an interpretation of what Flash said in the light of our own row, and storing an interpretation gives
you two fields that can disagree about one event.

## Where they show up

Not in `failing_syncs`, and not in `stale_syncs`. Both are bounded at 200 rows and neither is ordered, so
which rows come back is arbitrary once there are more than that — and abandoned checkouts are the one
failure that is both expected and unbounded in number. Left in, they would displace the credential error
or the lost paying subscriber those sections exist to surface, and all an operator would see is
`truncated: true`. `stale_syncs` is the worse of the two: dropping a row from the sweep freezes its
`last_synced_at` by design, so every abandoned row would age into "not read recently enough to trust"
within a day and stay there for good.

They get their own section instead, `abandoned_checkouts`, carrying pubkey, subscription id and
`sync_error_since`. Individually they are unremarkable. The count is the point: a spike is not a billing
fault but a broken checkout, and nothing else in the report would show it.

## Measuring "how long"

Giving up needs a clock, and none of the obvious columns is one:

| Column | Why it cannot answer "how long has this been failing" |
|---|---|
| `created_at` | When we first heard of the subscriber. A subscriber can sit legitimately `pending` for weeks, so row age would write them off on their first blip |
| `last_synced_at` | Stamped on every attempt, including the failures — a permanent failure looks permanently fresh |
| `updated_at` | Moves on every write, same problem |

Hence `user_subscription.sync_error_since` (migration `a1c4e7b02f19`): set when the current error first
appears, left alone while that same error repeats, cleared by a successful read. A *different* error
restarts it, because that is a different failure.

## The rule

A row drops out of `select_reconcile_candidates_on_db` only when all of these hold:

- `flash_status = 'pending'` and `granted_scheduling_id IS NULL` — nothing was ever granted, so nothing is
  at stake. A row that *did* grant and then goes unknown is a real anomaly; keep asking about it forever.
- `last_sync_error = 'unknown_subscription'` — Flash answered and had nothing. An outage or a credential
  failure raises rather than returning nothing, so those keep retrying.
- `sync_error_since` older than `billing_abandon_pending_after_seconds` (default 24h).

The sweep runs every `billing_sync_interval_seconds` (6h) over a bounded batch, so at the default that is
four further reads after the first unknown before the row drops out.

Every path that could revive the subscriber bypasses this query entirely: an `activated` webhook upserts
the row outright, `POST /user/subscription/refresh` reads Flash on demand, and the admin resync endpoint
does the same. Giving up on the sweep does not close the door.
