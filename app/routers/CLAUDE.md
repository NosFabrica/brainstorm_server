# app/routers

Every HTTP route lives here. One `router.py` per topic; subdirs nest by URL prefix.

## Aggregation

`router.py` (this directory, root level) is the single APIRouter that `app.api`
includes (`app/api.py:169`). Every other `router.py` in subdirs is `include_router`'d
here. To wire a brand-new endpoint, add the subdir + register it in this file.

## Top-level routes

| Path | File | Purpose |
|---|---|---|
| `/health` | `app/api.py:164` | Liveness (returns `1`) |
| `/whitelisted/{observer_pubkey}` | `router.py:75-94` | Trusted-pubkey list for an observer; `threshold` query param (default 0.02) |
| `/shortestPath` | `graph/router.py` | Path network: every shortest directed FOLLOWS path between two pubkeys; `only=hops` for Hops alone (ADR 0004) |
| `/networkAlerts` | `network_alerts/router.py` | Pubkeys carrying more verified reports than their reach justifies, split into direct-follows / extended-network |
| `/.well-known/nostr.json` | `nip05/well_known.py` | NIP-05 verification for Assistant pubkeys; resolution in `services/nip05_service.py` |

## Subdirs (by URL prefix)

| URL prefix | Subdir | Auth |
|---|---|---|
| `/authChallenge` | `auth_challenge/` | none (this IS auth) |
| `/setup` | `setup/` | none |
| `/admin/billing` | `admin/billing/` | `verify_token` + **`verify_billing_access`** — its own list, NOT `verify_admin_access`. Mounted outside `admin_router` on purpose: being on the billing list must not confer general administration, and turning admin routes off must not blind whoever handles payments. Also only mounted when `flash_enabled` |
| `/webhooks` | `webhooks/` | none — HMAC-signed by the sender. **Only mounted when `flash_enabled`** (see `include_billing_routers` in `router.py`), so it does not exist on deployments without payments |
| `/billing` | `billing/` | none — public plans list. **Always mounted**: an empty `plans` array is the "no billing here" signal the UI hides on. A mapping naming a service Flash does not hold drops that service's plans and logs an error, so one mistyped id degrades the page rather than refusing it |
| `/admin/billing/dev` | `admin/billing/dev.py` | billing access; mounted only when `flash_enabled` AND `deploy_environment == LOCAL` — mock Flash state (subscriptions AND plans, so the pricing page the paid rehearsal starts on has something to render) + signed synthetic webhook emitter |
| `/shorturl` | `shorturl/` | none — POST is rate-limited 1 req/s/IP |
| `/user` | `user/` | `verify_token` — **except** the `/user/{pubkey}*` lookups (see below) which are public, optional-auth |
| `/user/graperank` | `graperank/` | `verify_token` |
| `/user/support` | `support/` | `verify_token`. **Must be included before `public_user_router`** — otherwise `/{pubkey}` answers `GET /user/support` as a profile, 200 with the wrong body (pinned in `tests/test_support.py`). `GET ''` the state, `POST /tickets` to file, `GET /tickets/{id}` the thread (404 when absent *or* not the caller's), `POST /tickets/{id}/messages` to reply (rate-limited per pubkey; reopens a closed ticket), `POST /tickets/{id}/resolve` to close. The listing answers **304** to a matching `If-None-Match` — see the conditional-GET note below |
| `/admin` | `admin/` | `verify_token` + `verify_admin_access` |
| `/admin/brainstormPubkey` | `brainstorm_pubkey/` | (admin, included from `admin/router.py`) |
| `/admin/brainstormRequest` | `brainstorm_request/` | (admin, included from `admin/router.py`) |
| `/admin/users` | `admin/users/` | admin |
| `/admin/activity` | `admin/activity/` | admin |
| `/admin/stats` | `admin/stats/` | admin |
| `/admin/support` | `admin/support/` | admin. The queue also answers **304**. The responder side: `GET /tickets` the queue (paginated, filter by status/category/pubkey), `GET /tickets/{id}` any thread **with** `actor_pubkey` — the user-facing thread never carries it — `POST /tickets/{id}/messages` to answer (reopens a closed ticket), `/close` (optional note first), `/reopen`, and `PATCH` to recategorize |
| `/admin/graperank` | `admin/graperank/` | admin |
| `/admin/nsec-encryption` | `admin/nsec_encryption/` | admin |
| `/admin/trustedLists` | `admin/trusted_lists/` | admin |

## Authentication

Defined in `app/utils/api_validators.py` (not here).

- **`verify_token`** — FastAPI dependency. Accepts EITHER:
  - JWT bearer (`Authorization: Bearer <token>` or legacy `access_token` header), OR
  - **NIP-98** signed Nostr event (`Authorization: Nostr <base64-event>`).
  - On success sets `request.state.jwt_data: JWTData` (pubkey, is_admin).
- **`verify_billing_access`** — Defined in `admin/billing/router.py`. Checks `app.core.billing_admin_whitelist.get_billing_pubkeys()`, which falls back to the admin whitelist when `billing_admin_whitelisted_pubkeys` is unset. Deliberately **not** coupled to `admin_enabled`: these views carry payment data, and whoever answers "did my payment go through?" should not thereby inherit nsec key rotation.
- **`verify_admin_access`** — Defined in `admin/router.py`. Checks `request.state.jwt_data.nostr_pubkey` against the in-memory admin whitelist (`app.core.admin_whitelist.get_whitelisted_pubkeys`, hydrated at startup from `settings.admin_whitelisted_pubkeys`).
- Applied via `.include_router(..., dependencies=[Depends(verify_token), Depends(verify_admin_access)])` rather than per-handler.

Read `request.state.jwt_data` inside a handler to get the calling pubkey.

## Pagination

Two patterns:

- **`fastapi_pagination.Page[...]`** — used by `/admin/users`, `/admin/users/{pubkey}/history`, `/admin/activity`. Hooked into the app via `add_pagination(app)` (`app/api.py:177`). Repos build a SQLAlchemy `Select` and the router calls `paginate(db, stmt, transformer=...)`. A `Page` nested inside the success envelope (`GET /user/support`) is paginated in the service with explicit `params` under `set_page(Page[T])`, since the router's `response_model` isn't the `Page`.
- **Custom cursor pagination** — used by `GET /user/{pubkey}/connections` (cursor is a `(influence, pubkey)` tuple ordered by influence DESC, pubkey ASC). Implementation lives in `app/repos/user_repo.py::get_paginated_section_connections`.

Don't mix them: list endpoints over relational tables → `Page`. Graph-traversal endpoints → cursor.

## Response wrapper convention

Every successful response is wrapped via `SuccessfulResponseDataSchema` (in `app/schemas/request_response_schemas.py`):

```json
{ "code": 200, "data": { ... }, "message": "..." }
```

Each endpoint's specific response class (e.g. `GetUserDataResponse`) subclasses that wrapper and types `data` as the relevant payload schema. **Don't return a bare dict** — the contract is the wrapped shape.

**Exception: the admin surface returns raw shapes.** Admin routes follow the
PRD's admin convention — raw `response_model`, no success wrapper (`Page[...]`,
`/admin/billing`'s divergence/resync/plans/block bodies). The wrapper exists for
the app client's response handling; the admin UI reads models directly.

**Exception: `POST /user/sessions/revoke` is a bare 204.** Sign-out-everywhere has
nothing to report; the client only needs success, so there is no body to wrap.

**Exception: routes whose shape a third-party spec dictates.** Where an external protocol fixes the response body, the endpoint returns it unwrapped — the `.well-known` documents (`open_ranking/well_known.py` for ORE-01, `nip05/well_known.py` for NIP-05) via an explicit `JSONResponse`, and likewise the Open Ranking payloads. Wrapping any of them would break the spec. ORE-05 `/search/pubkeys` is a pass-through to the search relay (`open_ranking/search.py`): its body, status, `X-Reason` and `Retry-After` are the relay's.

## Per-router summary

### `auth_challenge/router.py` — Nostr login

- **GET** `/{pubkey}` → `NostrAuthChallengeResponse` (`data.challenge`). Issues a nonce, stored in Redis as `nostr:challenge:{nonce}` → pubkey for 5 min. A new challenge never invalidates an outstanding one.
- **POST** `/{pubkey}/verify` (body: `SubmitNostrAuthChallengeBody{signed_event}`) → `SubmitNostrAuthChallengeResponse` (`data.token`). Requires kind 22242, author = path pubkey, `t=brainstorm_login`, a valid id+sig, and a live challenge (from the event's `challenge` tag) issued for that pubkey; consumes it atomically (GETDEL), so it works once. Every failure is 401. No `created_at` check: freshness is the single-use 5-min challenge. Mints JWT. Logic lives in `services/auth_service.py`.
- Both are rate-limited per client IP (429 + `Retry-After`), plus a tighter per-IP hourly cap on verifies that create a new Observer. See `utils/CLAUDE.md` § rate_limiting.

### `setup/router.py` — Nostr pubkey setup

- **GET** `/{nostr_pubkey}` → kind-10040 designation rows (`list[list[str]]`): the five `30382:<tag>` rows, `30392`, then `39998` and `39999` at `settings.dictionary_relay` when it is set. Built by `services/duty_menu_build.designation_rows`, which the Duty Menu's offer also reads.

### `shorturl/router.py` — URL shortener

Short codes for an entity `(kind, key)`, stored in Postgres. See
[`shorturl/CLAUDE.md`](shorturl/CLAUDE.md) for the full design.

- **POST** `/` (body: `CreateShortUrlBody{kind, key, relays?}`) → `CreateShortUrlResponse` (`data.shortCode/kind/key/relays/path`). Rate-limited 1 req/s/IP. Idempotent per `(kind, key)`; first mint's relays win; max 7 relays.
- **GET** `/{short_code}` → `GetShortUrlResponse` (`data.kind/key/relays/path`). 404 if unknown; 422 if not `[A-Za-z0-9]{6,32}`. Codes never expire.

### `user/router.py` — user endpoints

Two routers in this file: `router` (authenticated, `verify_token` applied at
include time) and `public_router` (no include-level auth; each handler uses
`verify_token_optional`). The public router is included **after** the
authenticated one in `routers/router.py` so static paths like `/self` win over
the `/{pubkey}` catch-all.

**Authenticated (`router`)** — caller-private, require a token:

| Method | Path | Response | Notes |
|---|---|---|---|
| GET | `/graperankResult` | `GetOwnLatestGraperankResponse` | Latest result for caller |
| POST | `/graperank` | `GetOwnLatestGraperankResponse` | Triggers a run; throttled by `settings.block_frequent_graperank_requests_minutes` |
| GET | `/self` | `GetOwnUserDataResponse` | Caller's graph + history. Rows' `trusted_reporters` count above the caller's preset reporter cutoff |
| GET | `/subscription` | `GetSubscriptionResponse` | The caller's subscription as the UI shows it. Always answers, billing configured or not. `policy` is what they receive (their scheduling assignment — there is no tier string), `plan` is which one they bought (read through `billing_plan_id`) priced from the `pricingSnapshot` Flash recorded at their signup, never the plan's current listing — null price fields when Flash recorded no snapshot, never a substituted or zero one — the three dates come straight off the row, and `status` is the translated Flash vocabulary derived from `policy.is_default`. No `rail` — Flash exposes no payment method |
| POST | `/subscription/refresh` | `RefreshSubscriptionResponse` | Re-reads Flash for the caller and applies it — the redirect-landing call and the `pending` poll, which are the guide's two return paths. Optional body `{subscription_id?}`: given one, THAT subscription is verified with Flash; absent (a `pending` return carries none) the read is by reference. The id is a handle, not an authority — the reference it must carry is the caller's own pubkey, so a stranger's id changes nothing and discloses nothing. The redirect's `ref` is not accepted at all. The view carries `verification` (`verified`/`mismatch`/`unknown`/`not_given`/`unavailable`) so a refused id is not mistaken for a payment still confirming. An id naming a superseded subscription (an old redirect replayed after a re-subscribe) is decided from the reference read instead, never from the stale row. Per-pubkey rate limit |
| POST | `/sessions/revoke` | 204, no body | Sign out everywhere: bumps the caller's session version, so every token issued so far is a 401 |
| POST | `/assistantProfile` | `PublishAssistantProfileResponse` | Publishes kind-0 for the user's brainstorm assistant key |
| GET | `/assistantDutyMenu` | `GetAssistantDutyMenuResponse` | `{assistant, rows}`: what the assistant's Duty Menu offers now — `/setup`'s rows as `[key, relay]`, in its order, public relays, empty-relay rows dropped. **Must stay on this router**: on `public_router`, `/{pubkey}` would answer it as a profile with a 200, and Brainstorm-UI reads any 200 that isn't `{assistant, rows}` as "not supported here" |
| POST | `/assistantDutyMenu` | `PublishAssistantDutyMenuResponse` | No body. Reads the earlier versions strictly (502 when no relay answers), signs the kind-39999 menu (`d`, Nous' `z`, `p` = the assistant, one `duty` per GET row, `client`) dated `max(now, newest found + 1)`, checks it against § 6.1, and publishes to each row's relay (the internal TA relay for its public URL), the outbox (`assistant_relay_list()`), every relay an earlier version's rows named, and `wss://dcosl.brainstorm.world`. Answers `{event, relays: [{url, ok, message}]}` within the UI's 30 s; 502 when no relay took it |
| GET | `/assistantTreasureMap` | `GetAssistantTreasureMapResponse` | `{assistant, on, duties, shouldSay, relays}`, camelCase as Brainstorm-UI reads it: what the assistant's Treasure Map (its own kind 10040, a self-Map) should list now, and why. `duties` is one line per part of each of the caller's kind-10040 entries naming the assistant (`live`, `offered` with a `reason`, `not-offered`, `elsewhere`); `shouldSay` the `[key, relay]` self-entries, computed whether `on` or not; `relays` the outbox, the relays `shouldSay` names and the caller's hints for the assistant, for readers. 502 when the caller's Map can't be read, or, when the Map asks the assistant for Scores, the follow count (read alongside it, within 5 s); 422 when the Map names more than 64 keys. **Must stay on this router**, as `/assistantDutyMenu` |
| POST | `/assistantTreasureMap` | `PublishAssistantTreasureMapResponse` | Body `{on: bool}` (strict; anything else 422). Reads the newest version strictly at the outbox and the relays `shouldSay` names, never the caller's hints (502 when no relay answers), and publishes the self-entries alone (`shouldSay` when on, none when off), dated `max(now, newest found + 1)`, to those relays plus every relay an earlier version's self-entries named. When nothing would change it publishes nothing (`published: false`), unless a relay that answered the read lacks the newest version: that version then goes there unchanged (`published: true`). Saves the choice (`brainstorm_nsec.treasure_map_on`) only on success, committed before the answer. Answers `{on, published, event, relays: [{url, ok, message}]}` within the UI's 30 s; 502 when no relay took a new version (a repair answers `published: true` whether or not its relays take it: the version is already on the relay it was read from, and the report shows each failed line) or, turning on, the caller's Map, or the follow count when it counts, can't be read, 409 while another press for the same assistant is under way, 422 when the Map names more than 64 keys and the press would list duties |

**Public (`public_router`)** — optional auth. Observer perspective = caller's
pubkey when authenticated, else `app.utils.observer.default_observer_pubkey()`
(`settings.periodic_graperank_pubkey` or the hardcoded default). A *malformed*
token still 401s:

| Method | Path | Response | Notes |
|---|---|---|---|
| POST | `/trustSignals` | `GetTrustSignalsResponse` | Body `{pubkeys}` (≤500, 413 before per-item 422). Per pubkey: `/overview`'s `influence` + `flagged`, and `verified` on the same line. One UNWIND — for list surfaces that would otherwise call `/overview` per author |
| GET | `/{pubkey}/overview` | `GetUserOverviewResponse` | Lightweight counts + influence. The subject's own `verified` / `tier` and `flagged_by_observer` / `flagged_count` sit on the saved preset's verified line |
| GET | `/{pubkey}/stats` | `GetUserStatsResponse` | Per-section total + verified + tier breakdown. No query params — the tier bands are fixed constants |
| GET | `/{pubkey}/connections` | `GetUserConnectionsResponse` | Cursor-paginated; required `kind`, `limit`, `cursor`. `verified_only=true` filters on the section's own preset cutoff; `tier` uses the same fallthrough as `/stats`; each row carries the preset's `verified` verdict + `tier` |
| GET | `/{pubkey}` | `GetUserDataResponse` | Full 6-relationship graph. Rows' `trusted_reporters` count above the preset reporter cutoff |

The verified cutoffs, the verified line and the tier fallthrough all come from
the observer's **saved preset** (`get_verified_cutoffs` in
`user/dependencies.py`; `get_alert_cutoffs` in `network_alerts/dependencies.py`
for `/networkAlerts`, whose observer is a query param rather than the JWT
viewer), never from a client-supplied number — the
`verified_threshold` query param is gone from every read endpoint, and so
is `/connections`' `min_influence` — a client cannot supply a threshold at all.
`/{pubkey}` and `/self` take only the reporter cutoff. `/self` resolves it inline
via `resolve_verified_cutoffs`: it authenticates through `request.state.jwt_data`,
not the optional-token dependency `get_verified_cutoffs` hangs off.
`/stats` returns the verified *counts*; `/overview` returns no count of its own,
only the subject's own verdict and the two flagged fields; `/connections` rows
each carry their own `verified` / `tier`. All of them sit on the same line.

### `admin/billing/router.py` — billing visibility

Gated by `verify_billing_access` (see Authentication). Answers one question in
two halves: what Flash says we are charging someone, and what the scheduler
actually gives them. Where those disagree is the bug. It also carries the
operator's writes to Flash — cancel, pause and resume — each of which
re-reads the subscriber through `apply_entitlement` afterwards rather than
trusting the write's own answer.

| Method | Path | Notes |
|---|---|---|
| GET | `/subscriptions` | `Page[BillingSubscriptionItem]`. `flash_status` and `scheduling_name` are separate fields on purpose — collapsing them would hide the disagreement. `cancel_effective_date` is what separates "renews then" from "ends then", since a cancelled subscriber stays `active` until it lands |
| GET | `/divergence` | `DivergenceReportView`: ten named sections, each `{count, truncated, rows}` with a row model of its own so the OpenAPI schema says what each carries. Capped at 200 rows: a report nobody can open is no use on the day it matters |
| POST | `/subscriptions/{pubkey}/resync` | Re-reads one subscriber from Flash now |
| POST | `/subscriptions/{pubkey}/cancel` | Body `{reason?}`. Cancels in Flash on an operator's behalf — the support path, not the subscriber's, who still uses `portalUrl`. **Read `cancellation_scheduled`, never `flash_status`**: under the account's `end_of_period` policy Flash answers 200 with the subscriber still `active` and `cancelEffectiveDate` set, so deciding on the status reports a failure on a cancellation that worked |
| PATCH | `/subscriptions/{pubkey}/status` | Body `{status: "paused" \| "active"}`. Anything else is 422 — a cancellation has a reason and an effective date, and must not arrive through this door |
| POST / DELETE | `/subscriptions/{pubkey}/block` | Bar a user from paid entitlement / lift the bar. Blocking also revokes a billing-granted policy; admin grants are left alone |
| GET | `/unresolved/{subscription_id}/flash` | What Flash says about a signup that named nobody — its id is the only handle it has |
| GET | `/flash/services` | `FlashServicesData`: the services on our Flash account, read live — the plan editor's first picker |
| GET | `/flash/services/{service_id}/plans` | `FlashServicePlansData`: that service's plans as Flash offers them now (bypasses the public plans cache and rewrites it), each with `mapping_id` when a `billing_plan` row already claims it — the second picker. 404 when Flash holds no such service |
| POST | `/unresolved/{subscription_id}/attribute` | Body `{pubkey}`. Attaches a plain-link signup to whoever made it, by running `apply_entitlement` unaltered — never a hand-built grant. Refuses a pubkey that already holds a different subscription, and one already attributed elsewhere; re-attributing to the same person is a no-op |
| POST | `/unresolved/{subscription_id}/dismiss` | Writes it off as not a customer, granting nothing. Both outcomes settle every open event for that id with `resolution` + `resolved_by`, which stops the sweep and lets the row age out |
| GET / POST / PATCH | `/plans` | The `billing_plan` mappings — how dev and prod vaults get their rows. Two decisions only: which scheduling policy a Flash plan grants, and whether we sell it. Price, period, ordering and copy are read from Flash and are **refused** here rather than ignored, so a stale client cannot believe it set a price. The Flash ids are editable only while no `user_subscription` row references the plan — otherwise `409`, naming create-new-and-deactivate as the path, since rewriting them would retroactively change what subscribers bought |

Both subscription writes answer **409** when Flash declines the change (`FlashRefused`), which is not the 503 an unreachable Flash gets: there is nothing to wait for. A write that lands in Flash and then fails its own re-read still answers **200**, with `reason: "reread_failed"` — reporting it as a failure would be a lie about the one thing the operator most needs the truth about.

Attribute and dismiss both answer `UnresolvedResolutionOutcome`. `applied` false on an *attribution* is a decision, not a failure — a blocked user, a subscription already past its period, or one already attributed to that same person — so `entitlement_reason` carries the `EntitlementReason` behind it and a caller reports "nothing changed, and here is why" rather than a silent success. A dismissal runs no grant and so carries none. Nothing in either the subscription object or the webhook payload identifies the payer (no email, no name — verified against every event held; that is a claim about Flash's data, not the database — `support_ticket.notify_email` holds addresses), so who a signup belongs to is knowable only from Flash's own dashboard: that is why `/unresolved/{id}/flash` and the link out are part of this surface rather than a nicety.

### `graperank/router.py` — GrapeRank presets

| Method | Path | Notes |
|---|---|---|
| GET | `/preset` | Returns enum: DEFAULT \| PERMISSIVE \| RESTRICTIVE \| CUSTOM |
| PUT | `/preset` | Body: `SetGrapeRankPresetBody{preset}` |
| PUT | `/preset/custom` | 204; body: `GrapeRankPresetParams` (11 floats). Gated by `verify_admin_access` |
| GET | `/presets` | List all + caller's custom |

### `admin/...` — administrative endpoints

- **`admin/users`** — list users (paginated). Sort: `pubkey | times_calculated | last_triggered | last_updated`. `search` supports hex or npub. History: `/{pubkey}/history`. `POST /{pubkey}/resync` forces a full relay re-assert for that one observer. `POST /{pubkey}/sessions/revoke` kills that user's session tokens (users sign out everywhere themselves via `POST /user/sessions/revoke`).
- **`admin/activity`** — platform-wide request activity feed (paginated, filter by `status`, `algorithm`, `pubkey`, `days`).
- **`admin/stats`** — `GET /` → `AdminStats{total_users, scored_users, sp_adopters, queue_depth}`.
- **`admin/graperank`** — `PUT/GET /preset/{id}` for DEFAULT/PERMISSIVE/RESTRICTIVE; `GET /preset/{id}/history` for the audit log.
- **`admin/nsec_encryption`** — `POST /rotate` (202, background task — 409 if running) and `POST /verify` (returns `{ok, fail}` over all encrypted rows).
- **`admin/brainstorm_pubkey`** — `GET /{nostr_pubkey}` returns or creates the brainstorm pubkey (auto-triggers GrapeRank on creation). `POST /{nostr_pubkey}/trigger_graperank` to force one.
- **`admin/brainstorm_request`** — `GET /{id}` and `POST /` (body: `CreateBrainstormRequestBody{algorithm, parameters, pubkey}`).

## Common tasks

| I want to… | Do this |
|---|---|
| Add a new endpoint | Create `<topic>/router.py`, register in `routers/router.py`, define schemas in `app/schemas/`, business logic in `app/services/<topic>_service.py`, DB access in `app/repos/<topic>_repo.py` |
| Require auth on a new endpoint | `include_router(..., dependencies=[Depends(verify_token)])` |
| Require admin | Add `Depends(verify_admin_access)` (see `admin/router.py` for the pattern) |
| Get the caller's pubkey | `request.state.jwt_data.nostr_pubkey` |
| Paginate a SQL list | Build a `Select`, hand to `paginate(db, stmt, transformer=...)` |
| Paginate graph results | Follow the `get_paginated_section_connections` pattern (opaque tuple cursor) |
| Add custom CORS / middleware | `app/api.py` (CORS is wide-open at `["*"]` for dev) |

## Conditional GET on the support listings

Nothing is pushed and nothing is emailed, so a slow background poll is the only
way new activity reaches anyone. Both listings carry an `ETag` and answer a
matching `If-None-Match` with a bodyless 304 and no row read at all — the same
shape as `/whitelisted/{observer_pubkey}`, and like it, the 304 path is why the
handler is annotated `-> Response | …` while `response_model` pins the
documented shape.

The tag must cover **everything that varies what the client renders**, not just
new messages:

- `max(updated_at)` and the row count, so resolving, recategorizing and a
  message-less reopen all move it. Keyed on message activity those return
  "unchanged" forever and the poller stalls silently.
- `support_included` on the user listing, or a Policy being ticked stays hidden
  behind an unchanged ticket list.
- page, size and every filter, or page two comes back unchanged against page
  one's tag.

`Cache-Control: private, no-cache`: one caller's tickets are not a shared
cache's business.
