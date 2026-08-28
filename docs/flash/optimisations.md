# Optimisations and hardening not yet built

Things worth doing to the billing subsystem, with enough reasoning to decide
whether they are still worth doing when someone reads this later. Nothing here is
implemented.

## Rate-limit rejected webhook deliveries

`POST /webhooks/flash` is public and unauthenticated by our own JWT — the HMAC
signature is the authentication. Anyone who finds the URL can post to it as often
as they like, and today nothing counts how often they do.

**Only ever throttle deliveries that fail verification.** This is the whole
design and getting it backwards is worse than not building it: Flash retries a
failed delivery about three times and then **never replays it**. A 429 handed to
a legitimate delivery during a burst is an event lost permanently — a subscriber
who paid and never gets their tier. A valid signature proves the sender is Flash,
so a verified delivery must never be refused for rate reasons, however many
arrive.

So the limit hangs off the rejection path only:

```python
except FlashSignatureError as rejected:
    await note_rejected_delivery(request.client.host)   # 429s after N in the window
    logger.warning("Flash webhook rejected: %s", rejected.reason)
    raise HTTPException(status_code=rejected.status_code, detail=rejected.reason)
```

`app/utils/rate_limiting/rate_limiting.py` already has the mechanism —
`_enforce_window(key, limit, window_seconds)`, a Redis `INCR` plus `EXPIRE` that
raises 429 past the limit. This needs a third caller beside the IP and
billing-refresh limiters, not a new mechanism. Something like 20 rejections per 5
minutes per IP is generous for a misconfigured integration and tight for a flood.

**Keep the counter in Redis, never the database.** A forged delivery deliberately
leaves no trace — writing unverified bodies would turn a public endpoint into an
unauthenticated write primitive that anyone could use to fill a table. That rule
comes from the build spec in `.scratch/payments-flash/build/` at the workspace
root. An expiring Redis counter
keyed on IP keeps that property — nothing an attacker controls persists.

**The client IP is real, which is not obvious.** `request.client.host` would
normally be the ingress pod behind an NGINX ingress, making a per-IP limit either
useless or a way to block everyone at once. It works here because
`FORWARDED_ALLOW_IPS` is set to the pod CIDR (`10.244.0.0/16`, in each
`*-values.yaml`), so uvicorn trusts the ingress's `X-Forwarded-For` — which is
why the access log shows public addresses. Before relying on it for *blocking*,
confirm which element of a multi-hop `X-Forwarded-For` uvicorn's version takes; a
hostile client can send their own header and NGINX appends to it rather than
replacing it, so the wrong end of that list is attacker-controlled.

**Size the win honestly.** A junk request costs a body read and one HMAC over it,
and is rejected before any database work — so this is not the difference between
surviving a flood and not. The real gains are a ceiling on a cheap-but-nonzero
path, and keeping `logger.warning("Flash webhook rejected: …")` from burying
everything else in the log. Treat it as hygiene, not as the thing standing
between us and an outage.

A blunter option, if the traffic ever justifies it: an ingress-level limit on the
path, which never reaches the application at all. That has the same asymmetry
problem in reverse — the ingress cannot tell a valid delivery from junk, so it
would have to be loose enough never to touch Flash's own retries.

## Flag an `active` subscription whose period ended long ago

Nothing in the divergence report catches "Flash says `active`, the paid period
ended N hours ago, and no renewal event ever arrived". `stale_syncs` misses it
because we are reading those rows fine every cycle, and `policy_mismatch` misses
it because the policy matches what was granted.

That is the one signal separating "Flash is slow" from "we are giving away the
paid tier". It is not hypothetical: on 2026-08-27 four subscriptions sat `active`
17 hours past their `nextBillingDate` with no `subscription.renewed`, and nothing
would have told an operator. See [`api-observations.md`](api-observations.md).

Deliberately not an automatic revocation — `revoke_lapsed_entitlements` refuses
to judge those rows for good reason, since locally a missed renewal event is
indistinguishable from a failed renewal. A report section, not a rule.

## `updated_at` does not move on the sync path

`TimestampMixin` declares `updated_at` with `onupdate=func.now()`, which fires on
a normal `UPDATE` but not on `on_conflict_do_update` with an explicit `set_` — and
`upsert_user_subscription_on_db` does not list it. So on `user_subscription`,
`updated_at` means "last touched by something other than a Flash sync", and a row
re-read minutes ago can show a timestamp from days back.

Nothing queries it, so this is not a correctness bug. It is a trap for anyone
reading the table by hand, which during an incident is everyone. Adding it to the
values dict makes the column mean what the mixin promises; the cost is that it
then moves every sync cycle whether or not anything changed, which makes it close
to a duplicate of `last_synced_at`. Decide which meaning is wanted before
changing it.

## Surface `abandoned_checkouts` in the admin UI

The divergence report gained an `abandoned_checkouts` section and the endpoint
returns it, but no UI renders it. The count is the useful part — a spike is a
broken checkout flow rather than a billing fault, and nothing else in the report
would show that.
