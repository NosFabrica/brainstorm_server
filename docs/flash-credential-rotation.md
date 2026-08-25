# Rotating the Flash credentials

Two secrets, rotated differently, for a reason worth understanding before you
start: **we are the caller for one and the receiver for the other.**

| | What it is | Who signs with it | Overlap needed |
|---|---|---|---|
| `FLASH_API_KEY` | `sk_live_…`, account-wide, read scope | us, on every call | no — we choose which key we send |
| `FLASH_WEBHOOK_SECRET` | `whsec_…`, signs inbound deliveries | Flash, at the moment it sends | **yes** — see below |

The API key needs no overlap on our side: we simply start using the new one.
The webhook secret does, because Flash signs a delivery when it sends it, and a
delivery already queued under the old secret will arrive after you have swapped.
**Flash retries a rejected delivery only a few times and then never sends it
again**, so rejecting those is not a delay — it is permanent loss.

That is what `FLASH_WEBHOOK_SECRET_PREVIOUS` is for. While it is set, both
signatures are accepted. Clearing it is what ends the window.

---

## Rotating the API key

The urgent one: it is account-wide, shown exactly once at creation, and anyone
holding it can read every subscription on the account.

1. Flash dashboard → **Settings → API** → create a new key. Scope it
   `subscriptions:view` — the same as the one you are replacing. Copy it now;
   you cannot see it again.
2. Put it in the environment's secret (`brainstorm-flash-secrets`, key
   `FLASH_API_KEY`). See `brainstorm-k8s/kubeseal/README.md` for the sealing
   step, and **do not** run `seal-brainstorm-secrets.sh` — it regenerates every
   value in `brainstorm-secrets`, including the nsec encryption key.
3. Roll the server so it picks up the new value.
4. Verify (below) with `--skip-webhook`.
5. Only then, Flash dashboard → revoke the old key.

The order matters because we control which key we send: create first, revoke
last, and there is no moment where we hold no working key. **What the Flash
docs do not state is whether two keys can be active at once** — §6 only says a
key is shown once and that a lost one should be revoked and replaced. The
sequence above assumes they can. Prove it on staging before you need it: if
creating the second key silently invalidates the first, step 4 fails while the
old key is still deployed, which is the safe way to find out.

If step 4 fails, do not revoke anything — fix the secret and roll again.

## Rotating the webhook secret

1. Flash dashboard → **Settings → Webhooks** → rotate the signing secret. Copy
   the new `whsec_…`.
2. Set **both** in the environment's secret:
   - `FLASH_WEBHOOK_SECRET` = the new value
   - `FLASH_WEBHOOK_SECRET_PREVIOUS` = the value you are replacing
3. Roll the server.
4. Verify (below) with `--skip-api`.
5. Wait out the overlap. Deliveries signed with the old secret log
   `accepted on a superseded signing secret`. **When that line stops appearing,
   Flash has caught up.** An hour is generous; if you see none at all from the
   moment you rolled, there was nothing in flight.
6. Clear `FLASH_WEBHOOK_SECRET_PREVIOUS` and roll again. Clearing it without
   rolling changes nothing — the value is read at startup.

Step 6 is not optional housekeeping. A secret you rotated away from stays valid
for as long as it is set, which defeats the point of rotating. Every boot with
it set logs a warning naming this file, so a half-finished rotation announces
itself rather than waiting to be noticed.

## When the old value is already compromised

There is no safe overlap window, because the whole point is to stop honouring
the old secret immediately. Accept the loss and shorten it:

1. Rotate as above but **never set `FLASH_WEBHOOK_SECRET_PREVIOUS`.** Deliveries
   signed with the compromised secret are rejected from the moment you roll.
2. For the API key, revoke the old key in the Flash dashboard *first*, then
   create and deploy the new one. **Nobody is wrongly revoked in between** — a
   failed read never changes anyone's tier — but grants are delayed, not free:
   every delivery arriving in that window is recorded and acknowledged, then
   fails entitlement and is left unprocessed with the reason attached. They
   show as `unresolved_events` on the divergence report and settle on the next
   reconcile. Keep the window to minutes and check that list once it closes.
3. Expect loss on the webhook side. Flash gives up on rejected deliveries, and
   those events are gone.
4. **Recover deliberately** — this is what the reconcile path is for. Watch
   `GET /admin/billing/divergence`: `stale_syncs` and `failing_syncs` will show
   who was affected, and `POST /admin/billing/subscriptions/{pubkey}/resync`
   re-reads any subscriber from Flash directly. The periodic reconcile will get
   there on its own within `BILLING_RECONCILE_STALE_AFTER_SECONDS`, but during
   an incident you do not want to wait for it.

Rejected deliveries are logged as `Flash webhook rejected: invalid_signature`.
A burst of those right after a rotation means the old secret is still in flight
and you cut the window too early.

## Verifying

```bash
export FLASH_API_KEY=sk_live_...
export FLASH_WEBHOOK_SECRET=whsec_...
python -m scripts.check_flash_credentials --base-url https://<host>
```

Checks the two independently, so a failure names which one. The webhook check
signs a real delivery and posts it at the live endpoint — the same path and the
same verification a genuine delivery takes. It sends a `credential.check` event,
which is recorded like anything else but never interpreted, so a check cannot
change a tier or appear in the divergence report as an unmatched event.

Exit code is 0 only if everything it was asked to check passed. `--skip-api` and
`--skip-webhook` narrow it to one credential.

## Rehearse it before you need it

Run the whole webhook procedure on staging, including step 6, and confirm the
check passes at step 4 and again at the end. Rotate the API key there too — that
is what settles whether Flash keeps two keys active during a swap, which the
docs do not say and the sequence above assumes. That rehearsal is the only thing
that proves these steps — nothing here is exercised by the test suite, because
the failure modes are about which value is deployed where.

The one that bites is step 6: it is easy to leave `FLASH_WEBHOOK_SECRET_PREVIOUS`
set and believe the rotation is finished. Rehearsing means noticing that on
staging rather than discovering months later that a secret you thought you had
retired still works.

## What never happens

Neither credential is logged, echoed in a response, or written to the payments
tables. A rejected delivery names the failure, never the expected signature; a
Flash refusal names the status code, never the key or the response body. If you
ever need to compare values, read them from the secret — not from anything the
application emitted.
