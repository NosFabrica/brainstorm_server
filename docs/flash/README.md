# Flash billing

Working docs for the Flash payments integration. Code lives in
`app/core/flash.py` (the client), `app/services/billing_service.py` (entitlement),
`app/services/billing_sync_service.py` (the periodic half) and
`app/services/flash_webhook_service.py` (the receiver).

| Doc | What it answers |
|---|---|
| [`lifecycle.md`](lifecycle.md) | **Start here.** Every part that touches a payment, in the order it touches it — signup, the states a live subscription moves through, what repairs a missed event, and the rules that hold across all of it |
| [`pending-checkouts.md`](pending-checkouts.md) | Why a subscription row exists before anything is granted, what happens to a checkout nobody finishes, and how the reconcile sweep treats it |
| [`credential-rotation.md`](credential-rotation.md) | Rotating the API key and the webhook signing secret, including the compromised-key path |
| [`api-observations.md`](api-observations.md) | Where Flash's dev environment behaves differently from their documentation, with evidence, and the open questions for them |
| [`optimisations.md`](optimisations.md) | Hardening and cleanups worth doing but not built — webhook rate limiting, the missing "active past its period" check, and others |

## Source hierarchy

Get this right or you will build the wrong thing.

1. **`docs/PAYMENTS.md` in the workspace root** (alongside this repo, not inside
   it) — authoritative, written by Flash. Vault base URL, UUID ids,
   `redirect_uri`/`ref` params, HMAC-SHA256 webhooks.
2. `https://paywithflash.com/` — marketing site. Occasionally useful, never
   definitive.
3. `https://docs.paywithflash.com/` — **outdated and actively misleading.** It
   documents an older product: `app.paywithflash.com`, numeric `flashId`, base64
   `?params=` pre-fill, per-subscription JWT webhooks. Every one of those is
   wrong for our account. Do not answer questions from it.
4. The product spec and its decision record live in
   `.scratch/payments-flash/` — `PRD.md`, `issues/` for the decisions, `build/`
   for the implementation slices.

## The shape of it, in one paragraph

Identity is the `ref` query parameter, which is the user's hex pubkey — Flash
echoes it back in the redirect, in every webhook, and in the API response, which
is what makes attribution free. Entitlement **is** the scheduling assignment;
`user_subscription` only records why. Nothing is ever granted from a webhook
body: an event says something changed, and the API is then asked what it now is.
Uncertainty never costs a user their tier — an unreachable Flash, an unrecognised
status and an unmapped plan all leave the policy alone.
