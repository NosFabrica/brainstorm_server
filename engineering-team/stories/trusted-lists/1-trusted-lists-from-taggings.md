# Story 1: Trusted Lists from pubkey Taggings

**Status:** Draft (Gate A approved 2026-08-25)
**Created:** 2026-08-25
**Type:** Feature
**Source spec:** [NosFabrica/brainstorm_server#73](https://github.com/NosFabrica/brainstorm_server/issues/73)
**Classification:** ADR (irreversibility triggers: wire format / event shape,
schema + migration, cross-repo contract, auth-trust default) — story runs the
**Standard** phases per [`workflows/light-profile.md`](../../workflows/light-profile.md)
§"Gate A".

## Background

Tapestry (`nous-clawds4/tapestry`) already ships Trusted Lists: signed,
periodically-refreshed kind-30392 events whose membership is the
web-of-trust-filtered aggregation of kind-39999 *taggings* ("Avi is a
Podcaster"). Issue #73 ports that capability to brainstorm-server so that
@vitorpamplona can use TLs to augment keyword profile search.

The port is **not** a copy. Three things differ from tapestry:

1. **No pin layer.** Every tapestry TL derives from a *Pin* event, and the
   pin's `curation-method` JSON supplies `observer`, `cutoff`, and
   `includeScoreInTL`. Issue #73 skips pins entirely (they come to Brainstorm
   later) and derives the TL set from a usage-based **dictionary** instead.
   Those three inputs therefore have no wire source here and become server
   defaults.
2. **Per-customer signing keys.** Tapestry has exactly one owner TA key
   (`getOwnerAssistantKeys()`). This server mints one assistant nsec per
   observer (`get_or_create_brainstorm_observer_nsec_by_pubkey_on_db`,
   `app/repos/brainstorm_nsec.py:32`) and signs that observer's kind-30382
   TAs with it (`upload_nostr_events.py:473`). TLs follow the TAs.
3. **No local relay shell.** Tapestry reads taggings with
   `exec('strfry scan …')` from inside the relay container. This server never
   shells out to a relay, and `app/services/observer_sweep_service.py:8-13`
   records that REQ subscriptions recover only ~5% of a large set because
   strfry caps `maxFilterLimit` at 500. Taggings must therefore be *ingested
   and persisted*, not scanned on demand.

## User-facing description

As a Brainstorm admin, I want to trigger Trusted List generation for one
customer, so that every pubkey-Tag that customer's web of trust actually uses
becomes a published, signed, searchable list of the people who hold that Tag.

## Vocabulary

Per [`CONTEXT.md`](../../../CONTEXT.md) and tapestry's
`protocols/drafts/tags.md`:

- **Tag element** — a kind-39999 event declaring a category ("Podcaster").
  Addressable at `39999:<tagAuthor>:<slug>`; carries `slug` / `name` /
  `description` in its content payload. Tags with the same slug by different
  authors are **distinct elements**.
- **Tagging** — a kind-39999 event asserting a target pubkey belongs to a tag
  element. Carries `p` (target), `e` (tag element event id), `polarity`
  (apply / dispute), and a deterministic `d` making it replaceable — one live
  stance per (asserter, target, tag).
- **Dictionary** — for a given Observer, the set of tag elements used at least
  once by an asserter whose Rank in that Observer's web of trust clears the
  threshold. Issue #73 §3.
- **Trusted List (TL)** — the published kind-30392 event: one per dictionary
  entry, membership = the qualifying targets of that tag.

## Acceptance criteria

Testable from the outside. Each criterion gets at least one test handle.

- [ ] **AC1 — tag-element ingest.** A kind-39999 event carrying the tag
      concept `z` tag and a parseable payload is persisted with its event id,
      author, slug, name, description, and `created_at`. Re-ingesting the same
      addressable coordinate with a newer `created_at` replaces the stored row;
      an older one is ignored.
- [ ] **AC2 — tagging ingest.** A kind-39999 event carrying the
      `nostr-user-tag` concept `z` tag is persisted with its event id,
      asserter, target pubkey (`p`), referenced tag element (`e`), polarity,
      `d` tag, and `created_at`. Replaceability is enforced on
      (asserter, `d`): newer `created_at` wins, older is ignored — including a
      flip between apply and dispute.
- [ ] **AC3 — foreign kind-39999 ignored.** A kind-39999 event whose `z` tag
      matches neither concept address is not persisted to either table and
      does not error the consumer.
- [ ] **AC4 — malformed events ignored.** A tagging with no `p`, no `e`, or an
      unparseable payload, and a tag element with no slug, are skipped without
      raising; the consumer continues processing the rest of the batch.
- [ ] **AC5 — dictionary membership.** For a given Observer, the dictionary
      contains exactly those tag elements with ≥1 non-neutral tagging whose
      asserter's Rank in that Observer's web of trust is ≥ the configured
      threshold. A tag whose only taggings come from sub-threshold asserters is
      absent. A tag element referenced by no tagging is absent.
- [ ] **AC6 — rank threshold is the Observer's own.** The same tagging data
      evaluated under two different Observers yields dictionaries computed from
      each Observer's own per-observer Influence, not a shared/global score.
- [ ] **AC7 — polarity bucketing.** Polarity ≥ 0.5 counts as applied, ≤ −0.5 as
      disputed, values strictly between are dropped from both counts. An absent
      polarity tag defaults to applied.
- [ ] **AC8 — membership function.** A target is a member of a tag's TL iff
      `applications >= cutoff AND applications > disputes`. Members are ordered
      by applications descending, then pubkey ascending.
- [ ] **AC9 — TL wire shape.** Each published event is kind 30392 with: the
      `d` tag per ADR; `title` = the tag element's name; `description` = the
      tag element's description; `metric`; `observer`; `source-tag`; `cutoff`;
      `min-rank`; one `p` tag per member; and a `content` JSON carrying each
      member's applications/disputes counts.
- [ ] **AC10 — signing identity.** Every TL for Observer X is signed by X's
      assistant nsec — the same key that authors X's kind-30382 TAs — and by no
      other key.
- [ ] **AC11 — admin-only trigger.** `POST` to the trigger endpoint succeeds
      for a whitelisted admin pubkey and returns per-tag results. It returns
      403 for an authenticated non-admin, and 401 for an unauthenticated
      caller. The target Observer is a request parameter, not the caller.
- [ ] **AC12 — empty dictionary is a success, not a publish.** An Observer with
      no qualifying taggings returns HTTP 200 with an empty result list and
      publishes zero events.
- [ ] **AC13 — stale TL retraction.** A TL previously published for this
      Observer whose tag is no longer in the dictionary is replaced at the same
      `d` coordinate by an empty-membership event bearing a retraction marker.
      An already-retracted slot is not re-retracted (idempotent).
- [ ] **AC14 — a publish failure never wipes a live TL.** When publishing one
      tag's TL fails, that tag's `d` coordinate is still treated as current, so
      the retraction pass does not empty the healthy TL still on the relay. The
      failure is reported in the response for that tag and does not abort the
      remaining tags.
- [ ] **AC15 — the result is diagnosable.** The trigger response reports, per
      dictionary entry, the number of taggings considered and the number of
      members published. When the run produces nothing it distinguishes an
      empty tagging store (nothing ingested) from a populated store where no
      asserter cleared the rank threshold. This is the visibility mitigation
      ADR D1 accepts in exchange for the persisted-store design: an incomplete
      relay sync cannot be prevented downstream, but it must not be silent.

## Out of scope

- Pins and any user-facing curation surface (arrives with the later pin story).
- Scheduled / event-triggered recalculation — issue #73 §1 defers both to
  later versions; this story is the admin-triggered path only.
- Taggings of **content** (event targets) — issue #73 title note; the
  `nostr-event-tag` shape is unspecified upstream.
- Raising the dictionary threshold above "used ≥ 1 time", and the
  sum-of-rank alternative to an integer count (issue #73 §3 names both as
  future).
- `includeScoreInTL = true` — members publish without per-member scores in v1,
  matching tapestry's default.
- Consuming TLs in this server's own `/search` (the downstream augmentation is
  @vitorpamplona's, and a separate contract).
- Any UI. The admin button lives outside this repo.

## Open questions (to resolve during the ADR)

1. **Where are the taggings published, and does our relay receive them?**
   `NOSTR_TRANSFER_FROM_RELAY` is `wss://wot.grapevine.network`
   (`env.example:18`), a WoT/profile relay that is unlikely to carry
   kind-39999. Tapestry's taggings live on `dcosl.brainstorm.world` /
   `dcosl.brainstorm.social` via its opt-in `dcosl` router preset. Needs
   @vitorpamplona / David.
2. **Where should the TLs land?** Tapestry mirrors kinds 30392–30395 to
   `nip85.brainstorm.world` / `nip85.nostr1.com` / `nip85.grapevine.network`
   via its `trustedLists` preset. This server publishes TAs to
   `NOSTR_UPLOAD_TA_EVENTS_RELAY`. Same relay, or a distinct one?
3. **Does the out-of-repo strfry plugin forward kind 39999** onto the
   `strfry:events` Redis queue, or does it filter by kind? `process_strfry_event`
   ignores unknown kinds, which suggests a broad forward, but this is
   unverified and is a hard dependency of AC1/AC2.
4. **Does `GET /setup/{pubkey}` need kind-10040 designation rows for 30392?**
   It returns only the five `30382:*` rows today (`app/routers/setup/router.py:14-33`).

## Gate-command deviation (approved)

`poe check_all` cannot pass in this repo: the **baseline is red at HEAD**,
before this story's diff — `check_fmt` aborts the sequence with 38 files
needing reformat, and behind it sit 52 isort, 129 flake8 and 92 mypy findings
(see the 2026-08-25 OPEN.md row; the Light profile's "Instrument
preconditions" claim about this repo is false).

**Operator decision (Vinney, 2026-08-26): parity evidence substitutes for the
green gate for this story.** The inherited baseline is a different timeline's
responsibility; it may be fixed as a bonus on this branch or a separate PR.

Parity evidence, each stage run in the foreground with the exit code captured
by brace-redirect, this diff vs a stashed clean tree:

| Stage | Baseline (clean tree) | With this diff |
|---|---|---|
| `black --check` | 38 files | **36 files** (2 pre-existing files fixed in passing; failure set otherwise identical) |
| `isort --check` | 52 errors | 52 errors (identical set) |
| `flake8` | 129 findings | 129 findings (identical set) |
| `mypy` | 92 errors / 33 files (141 checked) | 92 errors / 33 files (**148** checked — 7 new files, zero new errors) |
| `pytest ./tests` | — | **550 passed, 0 failed** against the live local stack (Postgres, Neo4j, Redis, isolated strfry) |

The full suite's one prior "pre-existing failure"
(`test_relay_deletions_integration`) turned out to be environmental — it
fails against a populated foreign relay and passes against the clean isolated
one — so the honest baseline for the suite is green.

## Implementation scope notes (J3 item 2)

Two touches sat outside the blast radius as originally declared; both are now
named in the ADR's corrected Blast radius section, justified as follows:

- **`app/repos/user_repo.py`** gained one batched query,
  `get_qualifying_asserters_for_observer`. This is where the per-observer rank
  read (ADR D3) has to live: `app/neo4j_db/CLAUDE.md` rules that *all* Cypher
  lives in `user_repo.py`, so "a new repo module" could not lawfully hold a
  Neo4j query. The black reformatting of that file's untouched functions is
  incidental gate-parity work (it left the baseline failure set, see the
  deviation section above).
- **`app/schemas/trusted_list_schemas.py` (new) +
  `app/schemas/request_response_schemas.py` (two lines)**: the endpoint's
  response shape. Required by the repo's envelope convention — every wrapped
  response subclasses `SuccessfulResponseDataSchema` in
  `request_response_schemas.py` — and by `app/models/CLAUDE.md`'s rule that
  HTTP-boundary types live in `app/schemas/`, not in the service. The original
  blast radius simply forgot that a new endpoint implies its schema files.
- **`app/core/config.py`** gained four settings, not the declared three: the
  fourth, `trusted_list_relay`, is the ADR Q3 assumption's retarget-by-env
  knob, added when the operator ratified proceeding under that assumption.

Two planned handles (U9's 401-unauthenticated case; U13, AC14's per-tag
publish-failure isolation) were found dropped at J3 and are now implemented in
`tests/test_trusted_list_admin.py` — no removal is being defended; they were
owed and are paid.
