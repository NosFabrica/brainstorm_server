# Brainstorm Server Bible

> **Audience:** developers and AI agents joining this codebase. Read this to understand what the server *does* and *why it's shaped this way* — the narrative layer the reference docs don't carry.
>
> This document is deliberately thin, because everything else has a home: **concepts** (the trust model, points of view, what a score means) live in [CONCEPTS.md](https://github.com/NosFabrica/protocols/blob/main/CONCEPTS.md); **wire formats** in the [Trusted Assertions](https://github.com/NosFabrica/protocols/blob/main/specs/trusted-assertions.md) and [GrapeRank](https://github.com/NosFabrica/protocols/blob/main/specs/graperank.md) specs; **vocabulary** in [CONTEXT.md](./CONTEXT.md) (use its terms, avoid its avoid-lists); **code navigation** in [CLAUDE.md](./CLAUDE.md) and the per-directory CLAUDE.md files; **operations** (run, migrate, rotate keys, tune presets) in [README.md](./README.md). If a fact belongs in one of those, it goes there, not here.

**Last updated:** 2026-08-18 (initial version). *Maintenance rule: touch this file when you change the request lifecycle, a store's role or source-of-truth ranking, or the key model — and update the dated sections (§7) when their snapshot drifts.*

1. [What this server is](#1-what-this-server-is)
2. [The life of a Brainstorm request](#2-the-life-of-a-brainstorm-request)
3. [Stores, and who holds the truth](#3-stores-and-who-holds-the-truth)
4. [Keys and identity](#4-keys-and-identity)
5. [Ingestion and search](#5-ingestion-and-search)
6. [API surface](#6-api-surface)
7. [What's built, what's in progress](#7-whats-built-whats-in-progress)
8. [Design decisions worth knowing](#8-design-decisions-worth-knowing)
9. [Working on this codebase](#9-working-on-this-codebase)

## 1. What this server is

The production Brainstorm backend: a FastAPI service that ingests nostr events into a social graph, runs GrapeRank trust-score computations per **Observer**, publishes the results back to nostr as signed **Trusted Assertions**, and serves trust-aware profile search. It is the producer behind `api.brainstorm.world` and the search behind `search.brainstorm.world`; the [Brainstorm-UI](https://github.com/NosFabrica/Brainstorm-UI) is its main consumer, and any third-party client reading TAs per the spec is an equally legitimate one.

Its four responsibilities, in pipeline order: **ingest** (follows, mutes, reports, profiles, deletions — from the estate's relays), **compute** (schedule and dispatch GrapeRank runs to the Java worker), **publish** (sign and emit TAs, and keep the published set consistent as scores change), **serve** (search and the HTTP API).

## 2. The life of a Brainstorm request

The unit of work is a **Brainstorm request**: one full GrapeRank lifecycle for one Observer — calculate, then publish. Everything below is the path of one request; the state names are CONTEXT.md's.

**Trigger.** A request enters with a trigger source: *manual* (the Observer asks via `POST /user/graperank`, throttled), *admin* (forced via the admin routers), *scheduled* (the Observer's per-user schedule came due), or *periodic* (the house default-observer refresh; bypasses the Scheduler's admission gate).

**Admission and lanes.** The **Scheduler** — a leader-locked loop — admits overdue Observers as scheduled requests up to an in-flight target, counting in-flight scheduled requests as backpressure and yielding to interactive (manual/admin) runs. Admitted requests are `rpush`ed onto Redis **lanes** ([`app/services/scheduler_lanes.py`](app/services/scheduler_lanes.py)): with the scheduler off, everything shares `message_queue`; with it on, requests split by source — `sched:admin`, `sched:house` (periodic), `sched:{priority}` (scheduled; "priority is the lane") — while manual keeps `message_queue`.

**Calculation.** The Java worker ([`brainstorm_graperank_algorithm`](https://github.com/NosFabrica/brainstorm_graperank_algorithm)) pops a lane, fetches the Observer's neighborhood (hop-limited follow graph, previous influences) and the assertion edges (from the Redis reverse-set caches this server maintains), and runs the GrapeRank iteration exactly as [the spec](https://github.com/NosFabrica/protocols/blob/main/specs/graperank.md) describes. It returns a `GrapeRankResult` — scorecards keyed by Observee, plus the `changedScorePubkeys` incremental hint — on the `nostr_results_message_queue`. The request is *Waiting* until a worker pops it, *Ongoing* while calculating and publishing.

**The publish run** ([`app/message_queue_tasks/upload_nostr_events.py`](app/message_queue_tasks/upload_nostr_events.py)) turns the result into relay and search-index state:

1. Resolve (or create) the Observer's **assistant nsec** (§4) and connect a relay client.
2. **Sign** one kind-30382 TA per above-cutoff scorecard — only changed scores in steady state; every above-cutoff score under full-sync. Small batches sign sequentially; large ones shard across a process pool so signing doesn't starve the event loop.
3. **Plan deletions locally** (`plan_publish`): `fell_off = previously_published − currently_above_cutoff`, computed from the persisted baseline with no relay read. Dropped Observees get batched kind-5 `a`-tag deletions (≤ 200 coordinates per event).
4. **Publish to the relay(s), then mirror to Vespa** — in that order, always (§3).
5. **Persist the new published-state baseline.** On a clean run, exactly the above-cutoff set; if any sink write failed, the union of previous ∪ current, so an unconfirmed delete is re-issued idempotently next run instead of orphaning a score forever.

**Terminal states.** *Success* or *Failure*, with a parallel TA-publication status so a calculation that succeeded but never finished publishing is visible as exactly that. A non-terminal request whose worker died (deploy, crash — its lane payload popped and lost) is **Abandoned**; an admin **reaps** it terminal so it stops counting against admission. Reaping marks the row; it cannot resurrect, and a late worker write-back cannot un-reap it.

**Drift repair.** Steady state is incremental. Divergence between what the relay/Vespa hold and what the baseline believes is repaired by scheduled full-syncs (`FULL_SYNC_EVERY_N_RUNS`), per-run `force_full` overrides on the request row, or the admin resync — full-sync **re-asserts, it never decides deletions**. The below-cutoff sweep modes are backlog drains, deliberately not a steady state.

## 3. Stores, and who holds the truth

| Store | Holds | Truth ranking |
|---|---|---|
| **nostr relays** (strfry/neofry) | The published record: signed TAs, and the incoming assertion events | **Source of truth for published scores.** The publish is the act; everything else mirrors it |
| **PostgreSQL** | Domain state: requests and their lifecycle, encrypted assistant nsecs, presets and their history, the published-pubkeys baseline | Source of truth for the *process* (what ran, what was published, with which keys and parameters) |
| **Neo4j** | The social graph (`FOLLOWS`/`MUTES`/`REPORTS` per pubkey) + per-observer scorecard properties | Source of truth for the *input graph* |
| **Redis** | The lanes and result queues, plus reverse-set caches (`followed_by:` / `muted_by:` / `reported_by:`) | Transport and cache — rebuildable |
| **Vespa** | Profile docs with a per-observer `quality_scores` tensor | **A mirror, best-effort by policy.** Failures are logged, never propagated; drift is repaired by full-sync, not by trusting Vespa |

Two invariants follow, and code review should enforce them: **nostr publish before Vespa mirror** (never reorder — a mirror must not hold scores the relay never got), and **graph/Postgres writes must succeed while Vespa writes must not be allowed to fail a request**.

## 4. Keys and identity

Every Observer gets a dedicated **assistant key** (the "brainstorm nsec"): created on first need, stored AES-encrypted in Postgres (rotation, verification, and emergency runbooks: [README](./README.md) § Nsec encryption). All of that Observer's TAs — and their deletions, which relays only honor from the coordinate's author — are signed with it. One key per Observer is what makes the TA coordinate scheme work; treat it as an invariant, not a convention ([spec §Terminology](https://github.com/NosFabrica/protocols/blob/main/specs/trusted-assertions.md)).

The assistant is also given a public face: a kind-0 profile published on request (`POST /user/assistantProfile`), NIP-05 resolution for assistant pubkeys under this server's domain (`/.well-known/nostr.json`), and the setup endpoint (`GET /setup/{pubkey}`) that returns exactly the kind-10040 designation rows — `["30382:<tag>", <assistant pubkey>, <relay>]` — the UI publishes when an Observer activates Brainstorm as their provider.

## 5. Ingestion and search

**Ingestion** is the strfry-plugin path: events land on the `strfry:events` queue and dispatch by kind — 0 → Vespa profile upsert (partial, score-preserving), 3/10000/1984 → Neo4j relationship upserts + Redis reverse-sets (user-level reports only, per NIP-56), 5 → reconcile the deleting author's report edges. Details: [`app/message_queue_tasks/CLAUDE.md`](app/message_queue_tasks/CLAUDE.md).

**Search** is Vespa: each profile doc carries a sparse `quality_scores` tensor keyed by observer pubkey; `GET /search/byText` resolves the observer perspective (query param → authenticated caller → house default) and ranks with that observer's cell. The deployed application package lives in the kube chart (see CLAUDE.md § Vespa specifics — the one-click copy is stale). The search engineering essays in [`docs/`](docs/) are the deep dives, including the standing comparison against tapestry's Meilisearch implementation.

## 6. API surface

Route families, one line each — the OpenAPI page (`/docs`) is the contract, [`app/routers/CLAUDE.md`](app/routers/CLAUDE.md) the map: `authChallenge` (nostr login → JWT; NIP-98 also accepted), `user` (own results, trigger runs, assistant profile; public `/{pubkey}` overview/stats/connections with optional auth), `user/graperank` (presets), `search` (Vespa), `setup` (10040 designation rows), `graph` (`/shortestPath`), `networkAlerts`, `nip05` (`/.well-known/nostr.json`), and the `admin/*` families (users, activity, stats, presets, nsec-encryption, requests). Every wrapped response uses the `{code, data, message}` envelope; `.well-known` documents and other third-party-spec shapes are exempt by rule.

## 7. What's built, what's in progress

*Snapshot dated 2026-08-18 — re-date when you revise.*

**Built and load-bearing:** the full request lifecycle of §2 including scheduler lanes and reaping; incremental TA publishing with local-diff deletions and the self-correcting baseline; full-sync drift repair with per-run force overrides; the preset system (DEFAULT/PERMISSIVE/RESTRICTIVE/CUSTOM, DB-authoritative, with history); nsec encryption with rotation and verification; assistant kind-0 + NIP-05 + setup designation rows; per-observer Vespa search with graceful anonymous degradation; `/shortestPath`; `/networkAlerts`.

**In progress / known gaps:** token-less serving of house-POV profile data for anonymous UI visitors (coordinated with Brainstorm-UI — its anonymous profile pages 401 until this lands); the production `timestamptz` drift (CLAUDE.md § gotchas — normalize aware→naive at the listed boundaries); `ErrorResponseSchema` documented as the error convention but not used by live responses — three error shapes exist (tracked in [`engineering-team/OPEN.md`](engineering-team/OPEN.md)).

## 8. Design decisions worth knowing

- **Verified counts are preset-relative with no validity floor** ([`docs/adr/0001`](docs/adr/0001-verified-counts-preset-relative.md)) — a verified rater need not itself be publish-worthy. The one rule behind all three counts lives in the Java worker; the spec carries the semantics.
- **Deletions are computed from a local diff, never a relay read.** The persisted baseline (plus its dirty-run union rule) exists so the server never has to ask a relay what it previously published — and so a failed delete self-heals instead of orphaning.
- **Vespa is expendable by design.** Search rebuilds from the relay + kind-0 refeed; that's why its failures don't fail requests and why "treat Vespa as truth" is always a bug.
- **Full-sync and deletion are deliberately orthogonal.** Re-assertion repairs drift; the delete set comes from the diff regardless. Coupling them was rejected because a full-sync that also deleted would turn a drift-repair tool into a data-loss tool.
- **The engineering process is tapestry's harness, run by reference** — book-of-work artifacts land in [`engineering-team/`](engineering-team/), workflows followed from [tapestry's `engineering-team/`](https://github.com/nous-clawds4/tapestry/tree/main/engineering-team). One book (`shortest-path`) has run through it end-to-end.

## 9. Working on this codebase

Run it: [README](./README.md) (compose stack + `start.sh`; Vespa comes from the deployment repos). Checks: `poe check_all`; tests per README. Conventions (async-only, settings via Pydantic, lifespan-spawned consumers, the logger): [CLAUDE.md](./CLAUDE.md). Before naming anything, read [CONTEXT.md](./CONTEXT.md) and honor its avoid-lists. New working docs go in [`docs/`](docs/); decisions that constrain future work deserve an ADR (`docs/adr/` or the harness's `engineering-team/decisions/`); loose ends get a row in [`engineering-team/OPEN.md`](engineering-team/OPEN.md).
