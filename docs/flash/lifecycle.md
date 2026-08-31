# The payment lifecycle, end to end

Every part that touches a payment, in the order it touches it. The one path that
goes nowhere — a checkout nobody finishes — is
[`pending-checkouts.md`](pending-checkouts.md).

## The pieces

```mermaid
flowchart LR
    subgraph client["Brainstorm UI"]
        plans["Plans page"]
        ret["Return page"]
        sub["Subscription view"]
    end

    subgraph flash["Flash"]
        checkout["Hosted checkout"]
        api["Subscriptions API"]
        hooks["Webhook sender"]
        portal["Billing portal"]
    end

    subgraph server["brainstorm_server"]
        recv["Webhook receiver"]
        ent["billing_service, entitlement"]
        sweep["billing_sync_service, every 6h"]
        view["subscription_view_service"]
    end

    subgraph store["Postgres"]
        plan[("billing_plan")]
        usub[("user_subscription")]
        inbox[("flash_webhook_event")]
        nsec[("brainstorm_nsec.scheduling_id")]
    end

    plans -->|"checkout_url + ref"| checkout
    checkout -->|"redirect_uri, status"| ret
    ret -->|"POST /user/subscription/refresh"| ent
    hooks -->|"POST /webhooks/flash, HMAC"| recv
    recv -->|"record, then ack"| inbox
    recv --> ent
    sweep --> ent
    sweep --> inbox
    ent -->|"read by ref or id"| api
    ent --> usub
    ent -->|"the grant"| nsec
    plan -.->|"plan to policy"| ent
    view --> usub
    view --> nsec
    sub --> view
    sub -.->|"manage_url, cancel"| portal
```

Two things that diagram is trying to make obvious. **Entitlement is the write to
`brainstorm_nsec.scheduling_id`** — `user_subscription` only records why, and the
UI's `tier` is read back from the scheduling assignment rather than the billing
row, so it structurally cannot claim something the scheduler is not delivering.
And **every arrow into entitlement is followed by a read of Flash's API**: an
event says something changed, the API says what it now is.

## Signing up

```mermaid
sequenceDiagram
    autonumber
    actor U as Subscriber
    participant UI as Brainstorm UI
    participant S as brainstorm_server
    participant F as Flash

    UI->>S: GET /billing/plans
    S-->>UI: plans + checkout_url (no ref yet)
    Note over UI: the client appends ?ref=<pubkey>
    U->>F: opens checkout, pays
    F-->>U: redirect_uri?status=active|trial|pending&subscriptionId&ref
    U->>S: POST /user/subscription/refresh
    S->>F: GET /subscriptions?ref=<pubkey>
    F-->>S: the subscription, verbatim
    S->>S: decide, grant, upsert
    S-->>UI: status + tier

    F->>S: POST /webhooks/flash (subscription.activated)
    S->>S: verify HMAC, record, 200
    S->>F: GET /subscriptions?subscriptionId=…
    F-->>S: active
    S->>S: grant (idempotent — refresh may have done it already)
```

The `ref` is the user's hex pubkey and it is the whole identity mechanism: Flash
echoes it in the redirect, in every webhook, and in the API response. That single
parameter is what removed an entire correlation subsystem — email matching, a
30-minute window, an admin queue for unmatched payments.

Checkout is idempotent on `ref`, so reopening the link never double-charges. The
same property means **testing a second signup needs a different `ref`**, which
for us means a different pubkey.

Both paths converge on the same function and both re-read Flash first, so
whichever arrives second finds the work already done rather than redoing it.

## Once live

```mermaid
stateDiagram-v2
    state "pending — paying, unconfirmed" as Pending
    state "active / trial — entitled" as Active
    state "past_due — dunning, still entitled" as PastDue
    state "canceled — entitled until the date" as Canceled
    state "expired / paused — not entitled" as Ended

    [*] --> Pending: checkout returns pending
    [*] --> Active: checkout returns active
    Pending --> Active: subscription.activated
    Pending --> [*]: discarded, see pending-checkouts.md
    Active --> Active: subscription.renewed
    Active --> PastDue: subscription.past_due
    PastDue --> Active: a retry succeeds
    PastDue --> Ended: retries exhausted
    Active --> Canceled: subscription.canceled
    Canceled --> Ended: the paid period runs out
    Active --> Ended: subscription.expired
```

`past_due` keeps the tier on purpose — the subscriber is inside Flash's dunning
and has not lost anything yet; the UI shows it as `grace`. `canceled` keeps it
until `cancelEffectiveDate` or the period end, because Flash words cancellation
in the past tense and a date is what defers it.

Anything Flash sends that we do not recognise **holds**: an unknown status can
neither grant a tier nor take one away. That asymmetry is deliberate, since their
status set is documented as open.

## When something is missed

Flash retries a failed delivery about three times and then never replays it, so
none of the above can be the only path. Every cycle, in this order:

| Step | What it repairs |
|---|---|
| `replay_unprocessed_events` | An event we recorded and acknowledged but died before processing. Recorded before the 200 precisely so this is possible |
| `revoke_lapsed_entitlements` | A paid period that ran out and whose `expired` never arrived. Pure DB, no Flash call |
| `reconcile_subscriptions` | Everything only Flash can settle: `past_due`, `pending`, still-`active` past its period end, renewing within the hour, or simply not read in a while |
| `prune_webhook_payloads` | Personal data in stored payloads, past the retention window — see [Data retention](#data-retention) |

Two kinds of row leave `reconcile_subscriptions` for good, because re-reading
them can only return the answer we already have: a checkout Flash discarded
(see [`pending-checkouts.md`](pending-checkouts.md)) and an **expired
subscription holding no policy**. Without the second, the sweep's "not read in a
while" clause re-reads every subscriber who ever churned, once per cycle,
forever — a cost that grows with the life of the deployment. `paused` and
`canceled` rows keep being read: a pause is reversible and a cancellation is
still on its way to expiring. Anyone who comes back arrives as an `activated`
webhook, which upserts the row outright rather than going through the sweep.

The lapse sweep deliberately **refuses to judge** a `past_due` row, or an
`active` one past its period end. Locally those are indistinguishable from
"renewal succeeded and we missed the event", and revoking on that ambiguity would
cut off someone who is paying. They go to the reconcile step, which asks Flash.

There is one hole this cannot close, and it is worth knowing rather than
discovering: the sweep reads `user_subscription`, so **a subscriber with no row
is asked about by nothing**. Someone who paid, whose webhook was lost, and who
never returned to the redirect page is invisible permanently — and Flash offers
no endpoint that lists subscriptions — it answers only `?ref=` or
`?subscriptionId=` — so nothing can enumerate them either.

## Data retention

Every verified delivery is stored whole, body and all, in `flash_webhook_event`.
Flash's payloads carry the subscriber's `email`, `name`, `about` and
`picture_url` alongside the accounting fields — amounts, currency, invoice ids,
dates, the `externalRef` pubkey.

`prune_webhook_payloads` runs each cycle and **deletes those four keys** from
`payload.data`. It does not null the payload. Nulling would take the amounts
with it, and the amounts are what the accounting export reads — history would
quietly empty itself at the retention boundary. So what survives a prune is
everything except those four keys: the row, its dedupe key, the event type, the
timestamps, the attempt counters, and the whole accounting side of the body,
all of them indefinitely. Deleting rather than blanking also makes the job
idempotent — a key present but null still matches "has personal data" and would
be rewritten every cycle forever.

**Unprocessed events are exempt.** The prune only touches rows whose
`processed_at` is set. An event that named nobody we could match is still
waiting to be applied, and replay reads its payload to find the subscriber —
redacting it would make it permanently unreplayable. That exemption is what
keeps someone who paid but could not be attributed contactable: their email is
in that unresolved row, and it is the only place we have it.

`BILLING_PAYLOAD_RETENTION_DAYS` is **180**, and the number is the card dispute
window. A chargeback commonly arrives up to 120 days after the charge, so a
90-day window redacts the payer's name and email before a dispute can even
land, leaving us unable to answer it. 180 clears that with margin while staying
bounded: the outer edge for some dispute reasons is around 540 days, which is
close enough to "keep forever" that it stops being a retention policy at all.
Lightning payments are irreversible, so none of this protects that rail — the
window exists solely for the card one.

Be honest about the trade: this is a privacy cost, not a free win. It holds real
customers' email and name for twice as long as before. Changing the number is a
decision about which of those two risks you would rather carry, and the dispute
window is the thing to check before moving it.

## What can never happen

Rules that hold across every path above. Breaking one is a bug even where the
result looks right.

- **Nothing is granted from an event body.** Flash's own view is read first,
  which is also what makes concurrent handlers converge.
- **Uncertainty never costs a tier.** An unreachable Flash, an unrecognised
  status, an unmapped plan: all leave the policy alone.
- **The record and the grant commit together.** A tier without a record, or a
  record without the tier, is exactly what the divergence report exists to
  catch — so it should be impossible to create rather than merely detectable.
- **A delivery is recorded before it is acknowledged.** Once we answer 200 we own
  that event; a crash between the ack and a later commit would lose it for good.
- **Billing never erases an admin grant.** A comp survives a later lapse.
- **Only verified payloads are stored.** Writing unverified bodies would make a
  public endpoint an unauthenticated write primitive.

## Where to look when it is wrong

| Symptom | Look at |
|---|---|
| Paid but no tier | `flash_webhook_event` for their `externalRef`; then `failing_syncs` in `/admin/billing/divergence` |
| Tier but no payment | `policy_mismatch` and `admin_overrides` in the same report |
| "Confirming your payment" that never resolves | [`pending-checkouts.md`](pending-checkouts.md) |
| Never renewed | `flash_webhook_event` for a `subscription.renewed`; if absent, nothing was billed |
| An operator needs it fixed now | `POST /admin/billing/subscriptions/{pubkey}/resync` |
