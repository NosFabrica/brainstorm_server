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
| `/shortestPath` | `graph/router.py` | Shortest directed FOLLOWS path(s) between two pubkeys |
| `/networkAlerts` | `network_alerts/router.py` | Pubkeys carrying more verified reports than their reach justifies, split into direct-follows / extended-network |
| `/.well-known/nostr.json` | `nip05/well_known.py` | NIP-05 verification for Assistant pubkeys; resolution in `services/nip05_service.py` |

## Subdirs (by URL prefix)

| URL prefix | Subdir | Auth |
|---|---|---|
| `/authChallenge` | `auth_challenge/` | none (this IS auth) |
| `/setup` | `setup/` | none |
| `/search` | `search/` | none — see [search/CLAUDE.md → vespa](../core/CLAUDE.md) |
| `/admin/billing` | `admin/billing/` | `verify_token` + **`verify_billing_access`** — its own list, NOT `verify_admin_access`. Mounted outside `admin_router` on purpose: being on the billing list must not confer general administration, and turning admin routes off must not blind whoever handles payments. Also only mounted when `flash_enabled` |
| `/webhooks` | `webhooks/` | none — HMAC-signed by the sender. **Only mounted when `flash_enabled`** (see `include_billing_routers` in `router.py`), so it does not exist on deployments without payments |
| `/billing` | `billing/` | none — public plans list. **Always mounted**: an empty `plans` array is the "no billing here" signal the UI hides on. A mapping naming a service Flash does not hold drops that service's plans and logs an error, so one mistyped id degrades the page rather than refusing it |
| `/admin/billing/dev` | `admin/billing/dev.py` | billing access; mounted only when `flash_enabled` AND `deploy_environment == LOCAL` — mock Flash state (subscriptions AND plans, so the pricing page the paid rehearsal starts on has something to render) + signed synthetic webhook emitter |
| `/user` | `user/` | `verify_token` — **except** the `/user/{pubkey}*` lookups (see below) which are public, optional-auth |
| `/user/graperank` | `graperank/` | `verify_token` |
| `/admin` | `admin/` | `verify_token` + `verify_admin_access` |
| `/admin/brainstormPubkey` | `brainstorm_pubkey/` | (admin, included from `admin/router.py`) |
| `/admin/brainstormRequest` | `brainstorm_request/` | (admin, included from `admin/router.py`) |
| `/admin/users` | `admin/users/` | admin |
| `/admin/activity` | `admin/activity/` | admin |
| `/admin/stats` | `admin/stats/` | admin |
| `/admin/graperank` | `admin/graperank/` | admin |
| `/admin/nsec-encryption` | `admin/nsec_encryption/` | admin |

## Authentication

Defined in `app/utils/api_validators.py` (not here).

- **`verify_token`** — FastAPI dependency. Accepts EITHER:
  - JWT bearer (`Authorization: Bearer <token>` or legacy `access_token` header), OR
  - **NIP-98** signed Nostr event (`Authorization: Nostr <base64-event>`).
  - On success sets `request.state.jwt_data: JWTData` (pubkey, expiry, is_admin).
- **`verify_billing_access`** — Defined in `admin/billing/router.py`. Checks `app.core.billing_admin_whitelist.get_billing_pubkeys()`, which falls back to the admin whitelist when `billing_admin_whitelisted_pubkeys` is unset. Deliberately **not** coupled to `admin_enabled`: these views carry payment data, and whoever answers "did my payment go through?" should not thereby inherit nsec key rotation.
- **`verify_admin_access`** — Defined in `admin/router.py`. Checks `request.state.jwt_data.nostr_pubkey` against the in-memory admin whitelist (`app.core.admin_whitelist.get_whitelisted_pubkeys`, hydrated at startup from `settings.admin_whitelisted_pubkeys`).
- Applied via `.include_router(..., dependencies=[Depends(verify_token), Depends(verify_admin_access)])` rather than per-handler.

Read `request.state.jwt_data` inside a handler to get the calling pubkey.

## Pagination

Two patterns:

- **`fastapi_pagination.Page[...]`** — used by `/admin/users`, `/admin/users/{pubkey}/history`, `/admin/activity`. Hooked into the app via `add_pagination(app)` (`app/api.py:177`). Repos build a SQLAlchemy `Select` and the router calls `paginate(db, stmt, transformer=...)`.
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

**Exception: routes whose shape a third-party spec dictates.** Where an external protocol fixes the response body, the endpoint returns it unwrapped — the `.well-known` documents (`open_ranking/well_known.py` for ORE-01, `nip05/well_known.py` for NIP-05) via an explicit `JSONResponse`, and likewise the Open Ranking payloads and `nip50`'s NIP-11 document. Wrapping any of them would break the spec.

## Per-router summary

### `auth_challenge/router.py` — Nostr login

- **GET** `/{pubkey}` → `NostrAuthChallengeResponse` (`data.challenge`). Generates challenge, stores in Redis with TTL.
- **POST** `/{pubkey}/verify` (body: `SubmitNostrAuthChallengeBody{signed_event}`) → `SubmitNostrAuthChallengeResponse` (`data.token`). Validates signed event, mints JWT.

### `setup/router.py` — Nostr pubkey setup

- **GET** `/{nostr_pubkey}` → 30382 relay hints (`list[list[str]]`).

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
| GET | `/self` | `GetOwnUserDataResponse` | Caller's graph + history |
| GET | `/subscription` | `GetSubscriptionResponse` | The caller's subscription as the UI shows it. Always answers, billing configured or not. `policy` is what they receive (their scheduling assignment — there is no tier string), `plan` is which one they bought (read through `billing_plan_id`) priced from the `pricingSnapshot` Flash recorded at their signup, never the plan's current listing — null price fields when Flash recorded no snapshot, never a substituted or zero one — the three dates come straight off the row, and `status` is the translated Flash vocabulary derived from `policy.is_default`. No `rail` — Flash exposes no payment method |
| POST | `/subscription/refresh` | `RefreshSubscriptionResponse` | Re-reads Flash for the caller and applies it — the redirect-landing call and the `pending` poll, which are the guide's two return paths. Optional body `{subscription_id?}`: given one, THAT subscription is verified with Flash; absent (a `pending` return carries none) the read is by reference. The id is a handle, not an authority — the reference it must carry is the caller's own pubkey, so a stranger's id changes nothing and discloses nothing. The redirect's `ref` is not accepted at all. The view carries `verification` (`verified`/`mismatch`/`unknown`/`not_given`/`unavailable`) so a refused id is not mistaken for a payment still confirming. An id naming a superseded subscription (an old redirect replayed after a re-subscribe) is decided from the reference read instead, never from the stale row. Per-pubkey rate limit |
| GET | `/isSearchObserver` | `IsSearchObserverResponse` | Whether caller is searchable as an observer |
| POST | `/assistantProfile` | `PublishAssistantProfileResponse` | Publishes kind-0 for the user's brainstorm assistant key |

**Public (`public_router`)** — optional auth. Observer perspective = caller's
pubkey when authenticated, else `app.utils.observer.default_observer_pubkey()`
(`settings.periodic_graperank_pubkey` or the hardcoded default). A *malformed*
token still 401s:

| Method | Path | Response | Notes |
|---|---|---|---|
| GET | `/{pubkey}/overview` | `GetUserOverviewResponse` | Lightweight counts + influence. The subject's own `verified` / `tier` and `flagged_by_observer` / `flagged_count` sit on the saved preset's verified line |
| GET | `/{pubkey}/stats` | `GetUserStatsResponse` | Per-section total + verified + tier breakdown. No query params — the tier bands are fixed constants |
| GET | `/{pubkey}/connections` | `GetUserConnectionsResponse` | Cursor-paginated; required `kind`, `limit`, `cursor`. `verified_only=true` filters on the section's own preset cutoff; `tier` uses the same fallthrough as `/stats`; each row carries the preset's `verified` verdict + `tier` |
| GET | `/{pubkey}` | `GetUserDataResponse` | Full 6-relationship graph |

The verified cutoffs, the verified line and the tier fallthrough all come from
the observer's **saved preset** (`get_verified_cutoffs` in
`user/dependencies.py`; `get_alert_cutoffs` in `network_alerts/dependencies.py`
for `/networkAlerts`, whose observer is a query param rather than the JWT
viewer), never from a client-supplied number — the
`verified_threshold` query param is gone from all three read endpoints, and so
is `/connections`' `min_influence` — a client cannot supply a threshold at all.
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

Attribute and dismiss both answer `UnresolvedResolutionOutcome`. `applied` false on an *attribution* is a decision, not a failure — a blocked user, a subscription already past its period, or one already attributed to that same person — so `entitlement_reason` carries the `EntitlementReason` behind it and a caller reports "nothing changed, and here is why" rather than a silent success. A dismissal runs no grant and so carries none. Nothing in either the subscription object or the webhook payload identifies the payer (no email, no name — verified against every event held), so who a signup belongs to is knowable only from Flash's own dashboard: that is why `/unresolved/{id}/flash` and the link out are part of this surface rather than a nicety.

### `graperank/router.py` — GrapeRank presets

| Method | Path | Notes |
|---|---|---|
| GET | `/preset` | Returns enum: DEFAULT \| PERMISSIVE \| RESTRICTIVE \| CUSTOM |
| PUT | `/preset` | Body: `SetGrapeRankPresetBody{preset}` |
| PUT | `/preset/custom` | 204; body: `GrapeRankPresetParams` (11 floats). Gated by `verify_admin_access` |
| GET | `/presets` | List all + caller's custom |

### `admin/...` — administrative endpoints

- **`admin/users`** — list users (paginated). Sort: `pubkey | times_calculated | last_triggered | last_updated`. `search` supports hex or npub. History: `/{pubkey}/history`.
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
