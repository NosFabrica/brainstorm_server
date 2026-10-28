# app/routers/passkey_vault

The passkey vault: a passkey-locked copy of a user's key, so a passkey synced
through iCloud Keychain or Google Password Manager can sign them in on a device
that holds nothing yet. The server keeps ciphertext it cannot open and the
public key of the passkey that can. Why, and why it is safe:
[ADR 0005](../../../docs/adr/0005-passkey-vault.md). Logic in
[`services/passkey_vault_service.py`](../../services/passkey_vault_service.py),
assertion checks in [`utils/webauthn.py`](../../utils/webauthn.py).

URL prefix: `/passkeyVault`. Rows are addressed by `sha256(raw credential id)`
as 64 lowercase hex.

## Endpoints

| Method | Path | Auth | Body → `data` | Notes |
|---|---|---|---|---|
| POST | `/passkeyVault/challenge` | none, per-IP limit | → `{challenge}` | base64url `nonce ‖ issued-at ‖ HMAC`. Lives 2 minutes, spent once (Redis `SET NX`). Costs no storage to issue |
| POST | `/passkeyVault/{hash}/open` | none, per-IP limit — **the assertion is the credential** | `{assertion: {credentialId, clientDataJSON, authenticatorData, signature}}` → `{blob}` | Every failure is the same `404 {"detail": "Not found"}`: no row, bad signature, foreign or spent challenge, wrong origin, counter gone backwards |
| GET | `/passkeyVault` | `verify_token` | → `[{credentialIdHash, credentialId, label, createdAt, lastUsedAt}]` | the caller's passkeys, never a blob. Backs the Settings list, and the `excludeCredentials` of a new passkey |
| PUT | `/passkeyVault/{hash}` | `verify_token` | `{credentialId, publicKey, alg, blob, label?}` → entry | One `INSERT … ON CONFLICT DO UPDATE … WHERE pubkey = excluded.pubkey`, so concurrent first uploads settle in the database. `credentialId` must be canonical base64url and hash to the path; `publicKey` must load as SPKI of `alg`'s type (RSA: 2048–4096 bits, e=65537); `blob` (≤4KB, schema-checked) must be a v1 record naming the caller's pubkey and this `credentialId`. A row owned by another pubkey is a 403. A rewrite resets `sign_count` — the owner's way out of a counter their authenticator stopped keeping |
| DELETE | `/passkeyVault/{hash}` | `verify_token` | → `null` | Idempotent and owner-scoped: someone else's hash deletes nothing, and says so the same way |

## What open checks

- `clientDataJSON.type` is `webauthn.get`, its `origin` is one of
  `PASSKEY_ORIGINS` (empty = `FRONTEND_URL`).
- `rpIdHash` is `sha256(host of that origin)` — the UI creates every passkey with
  `rp.id = location.hostname`, so staging's passkeys open only staging's rows.
- User present **and** verified: the passkey stands in for a password.
- The signature, over `authenticatorData ‖ sha256(clientDataJSON)`, with the
  stored key (ES256, EdDSA, RS256).
- `crossOrigin` is not true: a ceremony inside someone else's iframe isn't ours.
- The challenge is ours and fresh (HMAC, 2 minutes, 5 s of replica clock skew)
  — checked before the signature, so a forged one costs no crypto.
- The counter advanced, when either side has one. Synced passkeys report 0. The
  write is conditional (`WHERE sign_count < n`), so two opens racing can't set
  it backwards.
- The challenge is spent (Redis `SET NX`) after the signature, so a forged
  assertion cannot burn somebody else's challenge.

## Why the public key comes in on the PUT

There is no registration ceremony to verify here. The PUT is made by the key's
owner, holding a Session, so a public key they hand us can only ever open a
vault holding their own key — a wrong one locks nobody out but themselves. That
is what spares the server CBOR and attestation parsing.
