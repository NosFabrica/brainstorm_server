# A passkey vault the server can hold but not open

Brainstorm-UI lets a passkey (Face ID, Touch ID, Windows Hello) stand in for the
Recovery password. On the device where the passkey was made, that needs nothing
from us: the browser seals the key under the passkey and keeps the result. On a
**new** device it needs the sealed copy to be somewhere the device can fetch
before it knows anything — which is what `passkey_vault` is.

## Status

accepted

## How the copy is sealed

The UI asks the passkey for its PRF output (the WebAuthn `prf` extension: an
HMAC the authenticator computes over a fixed input, after user verification,
inside the Secure Enclave or the password manager). HKDF turns that into an
AES-256-GCM key, which seals the Nostr key with the pubkey and credential id as
associated data. The PRF output never leaves the browser; the server receives
the ciphertext, the passkey's public key, and later only signatures.

So the server **cannot decrypt** what it stores, and neither can anyone who
reads its database. What a hostile server *can* do is withhold or delete a copy
(the user falls back to their key or Backup file), and see which pubkeys use
passkeys.

## Considered options

- **Publish the sealed copy as a Nostr event (NIP-78)** — no server work, but the
  ciphertext is public forever and fetching it needs no live ceremony.
- **Keep it on the device only** — the passkey then only replaces a password on
  the device that already has an Unlock cache, which is the case that needs it
  least.
- **This server, gated by an assertion (chosen).** Someone who takes over a
  user's Apple or Google account holds the synced passkey. A freely fetchable
  blob would hand them the other half too; behind an assertion over our own
  single-use challenge, getting it takes the same live, user-verified ceremony
  that produces the key.

## Consequences

- Writes take a Session, reads take the passkey. The public key arrives on the
  authenticated write rather than from a verified registration, which keeps
  CBOR and attestation out of the server; it can only lock its own owner out.
- Every failed read is one uniform 404, so the endpoint is no oracle for which
  credential hashes exist.
- `PASSKEY_ORIGINS` must list every UI origin whose passkeys should work. Each
  origin's host is its passkeys' relying party, so staging and production
  passkeys never open each other's rows.
- The real trust point is unchanged: whoever serves the UI's JavaScript could
  read a key after it is unlocked, as they can for every local-key login today.
