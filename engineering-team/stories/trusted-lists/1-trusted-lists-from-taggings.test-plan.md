# Test Plan: Story 1 — Trusted Lists from pubkey Taggings

**Story:** `engineering-team/stories/trusted-lists/1-trusted-lists-from-taggings.md`
**ADR:** `engineering-team/decisions/trusted-lists/0001-trusted-lists-from-taggings.md`
**Date:** 2026-08-25

## Coverage map

Every AC gets at least one handle. `U*` = fast suite (pure function, no
backend), `I*` = integration suite (real Postgres + Neo4j).

| Criterion | Handle | Test name | File | Level |
|---|---|---|---|---|
| AC1 tag-element ingest, newer replaces | U1, I1 | `test_tag_element_parsed_from_event`, `test_tag_element_upsert_is_latest_wins` | `tests/test_tagging_ingest.py`, `tests/integration/test_tagging_store_integration.py` | fast + integration |
| AC1 older `created_at` ignored | I2 | `test_tag_element_older_event_does_not_overwrite` | `tests/integration/test_tagging_store_integration.py` | integration |
| AC2 tagging ingest, full field extraction | U2 | `test_tagging_parsed_from_event` | `tests/test_tagging_ingest.py` | fast |
| AC2 replaceability on (asserter, `d`), incl. apply→dispute flip | I3 | `test_tagging_upsert_latest_wins_across_polarity_flip` | `tests/integration/test_tagging_store_integration.py` | integration |
| AC3 foreign kind-39999 `z` ignored | U3 | `test_unknown_z_tag_is_not_persisted` | `tests/test_tagging_ingest.py` | fast |
| AC4 malformed events skipped, consumer survives | U4 | `test_malformed_tagging_and_tag_element_return_none[…]` (5 cases: no `p`, no `e`, no `z`, unparseable payload, tag element with no slug) | `tests/test_tagging_ingest.py` | fast |
| AC5 dictionary = tags with ≥1 qualifying tagging | I4 | `test_dictionary_contains_only_tags_used_by_qualifying_asserters` | `tests/integration/test_trusted_list_integration.py` | integration |
| AC5 sub-threshold-only tag absent; unreferenced tag absent | I5 | `test_dictionary_excludes_subthreshold_and_unreferenced_tags` | `tests/integration/test_trusted_list_integration.py` | integration |
| AC6 rank threshold is per-Observer | I6 | `test_two_observers_get_different_dictionaries_from_same_taggings` | `tests/integration/test_trusted_list_integration.py` | integration |
| AC7 polarity bucketing incl. absent-defaults-to-apply | U5 | `test_polarity_bucketize[…]` (6 cases: 1, -1, 0.5, -0.5, 0.0, absent) | `tests/test_trusted_list_membership.py` | fast |
| AC8 membership predicate + ordering | U6 | `test_membership_requires_cutoff_and_net_positive`, `test_members_ordered_by_applications_then_pubkey` | `tests/test_trusted_list_membership.py` | fast |
| AC9 TL wire shape (all tags + content JSON) | U7 | `test_tl_event_wire_shape` | `tests/test_trusted_list_wire.py` | fast |
| AC9 `description` carried from tag element | U8 | `test_tl_carries_tag_description` | `tests/test_trusted_list_wire.py` | fast |
| AC10 signed by the Observer's assistant nsec | I7 → U18 + I11 | *substituted* — fast: `test_signing_pubkey_is_derived_from_the_observers_own_nsec` (`tests/test_trusted_list_admin.py`) asserts the key derives from the Observer's stored nsec via the same repo call the TA path uses; integration: I11's relay read-back is author-scoped to that signing key, so a wrong signer would return zero slots and fail every assertion. Recorded at J3 — the planned `test_tl_author_is_observer_assistant_pubkey` was never written under that name; coverage is equivalent, and this row now points at tests that exist. | fast + integration |
| AC11 admin-only trigger (200 / 403 / 401) | U9 | `test_trigger_requires_admin[…]` (3 cases) | `tests/test_trusted_list_admin.py` | fast |
| AC11 Observer is a path param, not the caller | U10 | `test_trigger_targets_path_observer_not_caller` | `tests/test_trusted_list_admin.py` | fast |
| AC12 empty dictionary → 200, zero publishes | U11 | `test_empty_dictionary_publishes_nothing` | `tests/test_trusted_list_admin.py` | fast |
| AC13 retraction only fires from a TRUSTWORTHY view (empty store / unscored Observer must retract nothing) | U17 | `test_untrustworthy_empty_view_never_retracts` | `tests/test_trusted_list_retraction.py` | fast |
| AC13 publish → retract → idempotent, end-to-end on a live relay | I11 | `test_publish_retract_idempotence_and_observer_scoping` | `tests/integration/test_trusted_list_publish_integration.py` | integration |
| AC13/S3 a run for one Observer never retracts another's slots | I11 | (same test) | `tests/integration/test_trusted_list_publish_integration.py` | integration |
| AC9 title + description on the actually-published event | I11 | (same test) | `tests/integration/test_trusted_list_publish_integration.py` | integration |
| AC14 publish failure does not trigger retraction of that slot | U12 | `test_failed_publish_keeps_dtag_current` | `tests/test_trusted_list_retraction.py` | fast |
| AC14 one tag's failure does not abort the rest | U13 | `test_publish_failure_is_isolated_per_tag` | `tests/test_trusted_list_retraction.py` | fast |
| AC15 per-tag counts in the response | U15 | `test_response_reports_per_tag_counts` | `tests/test_trusted_list_admin.py` | fast |
| AC15 empty store vs. no qualifiers distinguished | U16 | `test_empty_result_distinguishes_empty_store_from_no_qualifiers` | `tests/test_trusted_list_admin.py` | fast |

Level rationale: parsing, bucketing, the membership predicate, wire-shape
composition, and the retraction decision are pure functions over dicts — they
run in the fast suite with zero mocking, matching the existing
`tests/test_kind0_ingest.py` pattern of testing `_extract_kind0_profile`
directly. Anything asserting *storage* semantics (latest-wins) or *per-observer
trust* (Neo4j `influence_<observer>` properties) needs real backends and runs
in the integration suite — no mocked-session middle layer, per the existing
"tests must fail because the feature is missing, not because of scaffolding"
rule.

## Edge cases and regression sentinels

Checked items have a handle above or a named handle here; the rest are
explicitly not covered with a reason.

- [x] **`ev_kinds` sentinel (S1)** — `test_ev_kinds_still_only_graph_kinds`
      (`tests/test_tagging_ingest.py`): asserts `ev_kinds` contains exactly the
      five graph kinds and that 39999 is **not** among them, with a comment
      pointing at ADR D10. *Not derivable from any AC* — no acceptance criterion
      mentions `ev_kinds`. It exists because the naive implementation of D1
      silently disables the Redis relationship backfill via
      `_is_graph_db_populated` (`backfill_redis_relationships.py:28-36`), and
      that break is invisible from this story's own behavior. This is the
      sentinel that would catch a future contributor "tidying" the two lists
      back into one.
- [x] **Dangling tag reference (S2)** — `test_tagging_referencing_unknown_tag_element_is_dropped`.
      A tagging whose `e` points at a tag element we never ingested must not
      produce a TL with an empty title. Tapestry drops these at
      `profile-tags/index.js:1040` ("Drop tagEventIds whose tag-element isn't
      locally available"); no AC states it.
- [x] **Cross-observer retraction safety (S3)** —
      `test_retraction_never_touches_another_observers_tls`. A run for Observer
      X must not retract Observer Y's lists even though both live under
      different assistant keys at similar `d` coordinates. AC13 says "for this
      Observer" but does not test the negative.
- [x] **Exact-threshold boundary** — an asserter whose Rank is exactly
      `TRUSTED_LIST_MIN_RANK` qualifies (inclusive `>=`, ADR D4). Covered as a
      case in I4. This is the off-by-one D4 exists to pin down.
- [x] **`cutoff` boundary** — `applications == cutoff` is a member;
      `applications == disputes` is not. Cases in U6.
- [x] **Self-tagging** — an asserter tagging their own pubkey is counted
      normally (no special case), asserted in I4 so the behavior is recorded
      rather than accidental.
- [ ] **Not covered — `d`-tag collision from 8-char truncation.** Two tag
      authors sharing an 8-char pubkey prefix, or two Observers doing so, would
      collide on one TL slot. Inherited from tapestry's deployed shape and
      accepted in ADR D5. Not tested because the fix is a wire change, not a
      code change — testing it would only assert the known-bad behavior.
- [ ] **Not covered — concurrent triggers for the same Observer.** Two admins
      clicking simultaneously would double-publish to the same replaceable
      coordinates; last-write-wins makes the outcome converge. No locking is in
      scope for v1.
- [ ] **Not covered — very large tag membership.** Tapestry hit a 128 KiB
      `MAX_ARG_STRLEN` ceiling at ~600–700 members because it passed events as
      shell arguments (`trustedList/index.js:74-79`). We publish over a relay
      client, not a CLI, so that specific ceiling does not exist here. Relay
      message-size limits are a deployment property, not a unit under test.

## Error paths — one per external dependency

The design touches five external dependencies — note that the relay appears
twice, in both directions: the **read/sync** path that fills the tagging store
(D10's `tagging_ev_kinds` against `NOSTR_TRANSFER_FROM_RELAY`) and the
**write/publish** path that emits TLs. Vespa is deliberately absent
(ADR blast radius).

| Dependency | Failure | Handling | Handle |
|---|---|---|---|
| **PostgreSQL** | unreachable during the trigger | request fails loudly with a 5xx — Postgres is source-of-truth, so a silent empty dictionary would publish wrong (empty) lists | I9 `test_db_failure_aborts_before_publishing` — asserts **zero** publishes occurred |
| **Neo4j** | unreachable during rank lookup | same: abort before publishing, never treat "no score" as "below threshold" | I10 `test_neo4j_failure_aborts_before_publishing` |
| **Neo4j** | reachable, but the Observer has no `influence_<observer>` properties at all (never scored) | empty dictionary, HTTP 200, zero publishes — indistinguishable from "no qualifying taggings" and correct in both cases | U14 `test_unscored_observer_yields_empty_dictionary` |
| **Publish relay (write)** | unreachable / publish rejected | per-tag failure reported in the response; remaining tags continue; the failed tag's `d` stays current so retraction cannot wipe it | U12, U13 (AC14) |
| **Redis `strfry:events`** | queue empty | no-op; the consumer blocks on `blpop` as it already does for the five existing kinds | not covered — unchanged existing behavior, no new code path |
| **Transfer relay (read/sync)** | unreachable, or reachable but carrying no kind-39999 at all | the sync loop logs and continues; the tagging store stays empty or partial. **This is the design's accepted correctness risk** (ADR :111-115) — an un-synced relay yields a quietly incomplete dictionary, and no downstream assertion can tell "nobody tagged this" from "we never received the taggings". Mitigation is *visibility*, not correctness | U15, U16 |
| **Ingest input** | malformed / hostile event dicts | skipped without raising | U4 (AC4) |

### Making an incomplete sync visible (U15, U16)

The sync-path risk cannot be *tested away* — if the taggings never arrive, the
dictionary is correctly computed over an empty input. What can be tested is
that the operator can tell the two cases apart, so two handles exist for that:

- **U15 `test_response_reports_per_tag_counts`** — the trigger response carries,
  per dictionary entry, the number of taggings considered and the number of
  members published. A dictionary that quietly shrank between runs is then
  visible in the response rather than only in the published events.
- **U16 `test_empty_result_distinguishes_empty_store_from_no_qualifiers`** —
  when the run produces nothing, the response says *which* emptiness it was:
  zero taggings in the store (nothing ever ingested — the un-synced-relay
  case), versus taggings present but none clearing the rank threshold. AC12
  only requires that the empty case return 200 and publish nothing; this handle
  requires it to be diagnosable, which is what turns ADR :111-115's accepted
  risk from silent into merely unfixed.

Both are fast-suite handles over the service's return value, no relay involved.

**Level change recorded during implementation (J3 item 3).** I9 and I10 —
"read failure aborts before publishing" — were planned as integration handles
but implemented as fast handles in `tests/test_trusted_list_admin.py`. The
load-bearing assertion is *zero publishes occurred*, which is observable by
patching the publish boundary and needs no live backend; forcing a real
Postgres/Neo4j outage mid-test would add fragility without strengthening the
claim. No handle was dropped.

## Pre-implementation failure spot-check

Per J2 item 4, the plan must assert against code that does not exist yet.
Spot-check on **U6** (`test_membership_requires_cutoff_and_net_positive`): it
imports the membership predicate from the new service module, which is not in
the tree — `grep -rn "39999\|30392" app/` currently returns no matches, so the
import raises `ModuleNotFoundError` and the test fails at collection. The same
holds for U1–U14 and every integration handle. **S1 is the deliberate
exception:** it asserts a property of `ev_kinds` that is true *today* and must
stay true, so it passes before implementation by design — that is what a
regression sentinel is, and it is the only handle in this plan that does.

## Test infrastructure

- Framework: pytest; `integration` marker already registered in
  `pyproject.toml`.
- Fast suite: the existing `client` / `caller` fixtures
  (`tests/conftest.py:177-192`) plus the `admin_client` pattern from
  `tests/test_scheduling_admin.py:43-55`, which opens the admin gate by
  overriding `verify_admin_access`. AC11's negative cases override
  `verify_token` only, leaving the real admin gate in place.
- Integration suite: real Postgres (new tables via the story's Alembic
  revision) and real Neo4j at `settings.neo4j_db_url` — host port **7688** per
  the shortest-path plan's note, not 7687 (which belongs to the unrelated
  `tapestry` container).
- Fixtures: freshly generated keypairs per run (no collision with organic
  data), `DETACH DELETE` teardown for Neo4j nodes, transactional rollback for
  Postgres rows. Relay publishing is stubbed at the client boundary in the
  fast suite and asserted on call-count.
- No new test frameworks or dependencies.

## How to run

No Python 3.12/poetry on the host — run inside the server image with the
source mounted (same invocation as the shortest-path plan):

```bash
# fast suite (no services needed)
docker run --rm -v "$PWD":/app -v brainstorm-test-venv:/opt/.cache \
  brainstorm-server-service poetry run pytest -m "not integration" -q

# integration suite (needs the local stack up)
docker run --rm --network host -v "$PWD":/app -v brainstorm-test-venv:/opt/.cache \
  brainstorm-server-service poetry run pytest -m integration -q
```

Gate command for J3 is `poe check_all`, run in the foreground with the exit
code captured by brace-redirect — never piped through `tail`.
