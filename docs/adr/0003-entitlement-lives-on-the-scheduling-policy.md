# Entitlement lives on the scheduling Policy

Priority support is the first thing we sell that isn't recalculation cadence, so it forced a
decision the payments work never had to make: where does "is this user entitled to X?" live.
It lives on the **Policy** — a `scheduling` row — as a plain boolean column,
`scheduling.support_included`, read by one existing call.

```python
policy = await get_scheduling_for_pubkey_on_db(db, pubkey)
return bool(policy and policy.support_included)
```

The Policy is already the entitlement carrier, just not obviously named like one: it holds
`schedule_interval_seconds`, `manual_quota_limit`, `manual_quota_window_seconds`, `is_public`
and `enabled`. Those are entitlements in both shapes the literature names — limits and
toggles — and `support_included` is one more toggle beside them. `enforce_manual_quota` has
answered a per-Policy capability question this exact way since it shipped.

## Status

accepted

## Considered Options

- **A column on `billing_plan` (rejected).** The obvious home, since it's where the money is,
  and the one a future reader will assume we chose. But `billing_plan`'s own model comment
  says the Policy *"IS the tier: several plans may point at one policy (monthly beside
  yearly, a replacement beside the row it retires) and all of them grant identically"* — a
  capability on the mapping row lets monthly and yearly diverge, with nothing in the read
  path able to detect it. Worse, resolving it means reading `user_subscription`, whose
  docstring states it is *"never consulted to decide whether they are paid"*, plus a liveness
  gate on `granted_scheduling_id` and a separate fallback for admin-comped users who have no
  subscription row at all. Three clauses to answer one boolean.

- **A snapshot onto `brainstorm_nsec` (rejected).** Mirrors the existing `granted_scheduling_id`
  idiom: write what the user actually got at grant time, so the read is one column on their own
  row. Sound, and the right answer *if* the rule lived on `billing_plan`. Once the rule moved to
  the Policy the read was already a single call, so the snapshot bought nothing and cost a write
  path through `resolve_entitlement`, a second timing model (ticking a box would stop being
  retroactive), and a new way for two columns to disagree.

- **A `capabilities` JSONB map on the Policy (rejected, for now).** The general form: one
  generic column, the catalog as a Python enum, no migration per feature. Correct destination
  and the one to reach for when a second capability appears — but speculative at one paid
  Policy and one capability, and it trades a typed column for a blob the DB cannot check.
  Migrating into it later is mechanical: one column becomes one key.

- **A per-user `capability_grant` table (rejected).** Needed for add-ons and trials, since it
  is the only shape that survives a capability sold separately from the Policy. Its `source`
  and `subscription_ref` columns restate what `user_subscription` already records, which is
  the tell that it wants designing alongside the billing model rather than bolted onto a
  support feature.

## Consequences

- **Ticking the box is retroactive and instant.** It is read live off the Policy, so every user
  on that Policy is entitled on their next request — no re-grant, no resync, no sweep.
- **Billing is untouched.** No change to `billing_plan`, `user_subscription`, `billing_service`
  or `resolve_entitlement`, and the support endpoints never read any of them. Support works on
  a deployment with `flash_enabled=false`.
- **Comps need no new surface.** Assigning a user to a Policy that has the capability is
  already `PUT /admin/users/{pubkey}/scheduling`, which is what `SchedulingSource.ADMIN` means.
- **Two Policies that differ only in a capability need two `scheduling` rows** with the same
  interval. The scheduler is indifferent to that and `/billing/plans` correctly renders them as
  two products; the cost is duplicated cadence, so a 7d→5d change edits both rows. Fine at a
  handful of Policies. If it ever stops being fine, the fix is a separate entitlement table
  pointing at a shared `scheduling` row.
- **Add-ons are not expressible**, and not for want of a column. There is nowhere to hang a
  second, independently-billed grant: the grant is single-valued (`scheduling_id` → one
  Policy), `user_subscription.pubkey` is the primary key so there is one subscription row per
  user by construction, and `resolve_entitlement`'s REVOKE is all-or-nothing with no notion of
  dropping one capability and leaving the Policy. Selling an add-on is a payments change, not
  an entitlement one.
- **`is_support_allowed` is the single seam.** Every alternative above — capability map,
  per-user overlay, add-on union — changes that one function body and nothing else.
- Entitlements stay distinct from the `*_enabled` settings in `config.py`, which are deploy
  flags: "does this installation have X", not "did this account pay for X". A flag must never
  decide billing access, which is why the column is `support_included` and not
  `support_enabled`.
