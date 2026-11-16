# Authenticating to the Brainstorm API

API base URL: `https://api.brainstorm.world` (staging: `https://brainstormserver-staging.nosfabrica.com`).

| | Session login | NIP-98 per request |
| --- | --- | --- |
| Use for | First sign-in, and apps holding a session | Registered users, stateless calls |
| Creates the account | Yes | No (401 if the pubkey has never logged in) |
| Event kind | 22242 | 27235 |
| Sent as | `POST /authChallenge/{pubkey}/verify` body | `Authorization: Nostr <base64 event>` header |

A new user must sign in with a session login first: accounts are created only on `/verify`, which is challenge-gated and capped per IP. Each method accepts only its own kind.

## Session login

1. `GET /authChallenge/{pubkey}` (hex) returns `{"data": {"challenge": "<hex>"}}`. It is short-lived and works once.
2. Sign a kind 22242 event with empty content and these tags. `client` is optional but please send it: it is how we attribute users to apps.

   ```json
   [["t", "brainstorm_login"], ["challenge", "<challenge>"], ["client", "<your app name>"]]
   ```

3. `POST /authChallenge/{pubkey}/verify` with `{"signed_event": <event>}` returns `{"data": {"token": "<jwt>"}}`.
4. Send `Authorization: Bearer <jwt>`. Sign in again on a 401.

## NIP-98

Follows [NIP-98](https://github.com/nostr-protocol/nips/blob/master/98.md): kind 27235, `created_at` within 60 s of server time, tags `u` (exact URL, query included), `method`, and `payload` (sha256 hex of the body) when there is a body.

## Errors

Auth failures are `401` with a `detail` message. Rate limits are `429` with `Retry-After`.

## Getting a user scored

1. `POST /user/graperank` queues a score run.
2. `GET /user/history` returns `ta_pubkey`, the key the user's kind 30382 scores are signed with.
3. `GET /setup/{pubkey}` returns the rows the client publishes in the user's kind 10040.
