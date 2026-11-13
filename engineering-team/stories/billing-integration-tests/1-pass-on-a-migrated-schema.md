# Story 1: The five billing integration tests pass against a migrated schema

**Status:** Done. Gate B passed on 2026-11-13
([review](../../reviews/billing-integration-tests/1-pass-on-a-migrated-schema.md)). Built on
[NosFabrica/brainstorm_server#142](https://github.com/NosFabrica/brainstorm_server/pull/142): the fix is `a9e531e`,
and the review's fixes are `c836b55`.
**Created:** 2026-11-13, after the build
**Type:** Bug. Existing behaviour is wrong: five integration tests fail whatever the setup.
**Found by:** the Gate B re-review of [#120](https://github.com/NosFabrica/brainstorm_server/pull/120), which ran the
README's integration steps and got 161 of 166.
**Classification:** Bug lane (Light profile, trial). The change touches two files under `tests/integration/` and
nothing else, so no irreversibility trigger applies: no wire format, auth or trust default, schema or migration,
dependency, cross-repo contract, routing, header, or value shared between repos. A Design note, not an ADR.

## How this story was run

An authoring Claude session found the five failures, fixed them, and ran the checks at the end of this file before
writing it. So the file records the fix rather than planning it. An independent Reviewer runs Gate B, and the operator
gives its verdict.

## Background

CI deselects the `integration` marker (`.github/workflows/build.yml:58`), so integration tests run only when someone
runs them by hand against real stores. Five billing tests had drifted from the code and schema they test:

- `test_billing_plan_write_integration.py` came with `73ca8bc` ("a plan edit no longer 500s after saving"). It guards
  against a `MissingGreenlet`: a plan write's response read `updated_at` after the UPDATE had expired it.
- The four tests in `test_billing_reconcile_candidates_integration.py` came with `8ddcebd`, `168212d`, `06bb99b` and
  `320c167`. They pin which subscriptions the reconcile sweep reads, how the sync-error clock runs, and which
  subscriptions the failing, stale and abandoned sections list.

## The bug

Against a schema migrated to head, with any settings:

1. The plan-write test calls `create_billing_plan` with `amount_minor` and `currency`. `a8d0e40` dropped both from
   `billing_plan` when plan pricing moved to Flash, so the call fails:
   `insert_billing_plan_on_db() got an unexpected keyword argument 'amount_minor'`. Its edit set `includes`, another
   dropped column. So even with the create fixed, the edit wouldn't have issued an UPDATE, and the test would no longer
   reach the `MissingGreenlet` path.
2. The four reconcile tests seed `user_subscription` rows with a NULL `billing_plan_id`, which the column has never
   allowed: a NOT NULL violation. They also grant policy 4, and a fresh migration creates only one policy (`Weekly`).
3. In suite order, the first reconcile test fails with "Event loop is closed" instead. The plan-write test before it
   never disposed the shared engine, so its pooled connections were still bound to its own event loop.

## Acceptance criteria

- **AC1.** Against a schema migrated to head, the five tests pass alone, together, and in the full integration run
  (`pytest -m integration`).
- **AC2.** Each test still fails with its own message when the behaviour it covers is broken in `app/`. Each line gives
  the edit, then the message:
  - `update_billing_plan` without `db.refresh(plan)`: `MissingGreenlet`, in the plan-write test;
  - `record_sync_failure_on_db` setting `sync_error_since` to now on every failure: "a repeat restarted the clock";
  - `select_reconcile_candidates_on_db` without its abandoned exclusion: "abandoned checkout still swept";
  - `select_failing_syncs_on_db` without its abandoned exclusion: "abandoned checkout crowds the failures";
  - `select_reconcile_candidates_on_db` without its settled exclusion: "still re-reading a settled subscription";
  - `AbandonRule.condition` and `.matches` ignoring the grant: "stopped watching a subscriber with a policy";
  - `settled_condition` ignoring the grant: "stopped watching a live tier".

  The last two pin the probes this change rewrote, the ones that hold a policy (`granted=True`).
- **AC3.** The probes rely only on what a fresh migration creates: no fixed policy id, and no column the schema
  doesn't have.
- **AC4.** No test leaves a probe row behind, whether it passes or fails. Each deletes them before it starts, and again
  in a `finally` when it ends.
- **AC5.** The change is test-only: nothing outside the two files changes, and `poe check_all` gives the same result as
  on `staging`.

## Design note

- **Chosen:** every reconcile probe sits on one probe plan (`svc-reconcile-probe` / `plan-reconcile-probe`). The plan is
  made on first use and mapped onto the lowest-numbered existing policy, and the probes that hold a policy hold that
  one. None of the queries under test reads the plan, so which policy it maps to doesn't matter.
- **Chosen:** the plan-write test creates a mapping from the columns a mapping has (service, plan, policy, on sale), and
  its edit takes the mapping off sale. The edit changes a real column, so the UPDATE runs and expires `updated_at`,
  which is the path the test guards.
- **Chosen:** every test ends in a `finally` that removes its probe rows and disposes the shared engine. The plan-write
  test gained the disposal, which the reconcile tests already had. After review, the reconcile tests gained the
  cleanup, which until then ran only when a test passed.
- **Rejected:** creating a policy with id 4 in the tests. It would keep the old numbers, but it ties the probes to an id
  a fresh migration doesn't make, and that id could collide with a real policy on a database that has more.
- **Rejected:** making `billing_plan_id` nullable. The tests would pass, but production always has a plan when it writes
  a subscription (`_grant_and_record`), so the tests would pin a state production never creates.
- **Blast radius:** two test files. The probes write only rows they own (their own pubkeys, service ids and plan ids),
  and delete them, a failed test's included. They read the first `scheduling` row and change nothing in it. While a
  test runs, though, its probe plan is an on-sale mapping onto that policy, so a server reading the same database leaves
  that policy's free row out of `/billing/plans`.

## Out of scope

- Running integration tests in CI.
- The billing code itself, in which these tests found no fault.
- black's layout of the reconcile file. `check_all` lints only `app/`, and `staging` already has that layout.
- The other 161 integration tests.
- PR #120's README paragraph saying these five fail "until #142". Once both have landed it's stale, and whichever of
  the two merges second removes it.

## Edge cases and regression sentinels

- Within a test, every `_seed` call after the first reuses the probe plan the first one made, instead of inserting a
  second plan.
- A database with no `scheduling` row stops the probe setup with "no scheduling policy to map the probe plan onto",
  rather than with a foreign-key error.
- A database with several policies: the probes use the lowest id, and pass whichever it is.
- Cleanup order: the probe subscriptions are deleted before the probe plan they reference.
- Suite order: the plan-write test runs just before the reconcile file, so a leaked connection shows up there first.
- A database that already holds real billing rows: the abandoned-section check compares only the probes, so a real
  abandoned checkout doesn't fail it.
- A test that fails midway leaves nothing behind. Its cleanup runs in a fresh session, which still works when the
  test's own session failed on a statement.

## AC→handle lines

- AC1 → `pytest -m integration` against stores migrated to head; also each file alone, and the pair.
- AC2 → each of the seven edits AC2 lists, made in `app/` one at a time: run both files, expect that message, restore.
- AC3 → `_probe_plan` and `_seed` in the reconcile file; the plan-write test's `_policy_id` and its create call.
- AC4 → each file's `_cleanup`, called at the start of every test and again from its `finally` (in the reconcile file,
  through `_finish`); then, after a test fails on purpose, a count of the probe rows left.
- AC5 → `git diff --stat a896204...` on the branch: the two test files, plus this story and its review;
  `poe check_all`'s five commands.

## Checks (the implementer's, before review)

Run on 2026-11-12, against throwaway Postgres, Redis, Neo4j and strfry migrated to head. The settings came from
`env.example`, pointed at those stores, less the three Flash keys.

- AC1: 166 passed. Each file alone passed, and so did the pair. Without the fix, 161 passed and these 5 failed.
- AC2: all five behaviours were caught, each with its test's own message, and `app/` was restored after each.
- AC5: black, isort, flake8 and mypy are clean on `app/`. `pytest -m "not integration"` gave 1,792 passed,
  166 deselected and 9 warnings, as on `staging` at `a896204`.
- AC4: not run as a check of its own. The runs above shared one database in sequence and each started clean, but no
  run came straight after a deliberate failure midway through a test.

## Checks after the review (2026-11-13)

Run on the fixed tests (`c836b55`), against a fresh throwaway stack migrated to head, with the same settings:

- AC1: 166 passed, both before and after the checks below, on the same database. Each file alone, the pair, and the
  pair in reverse order passed.
- AC2: all seven edits were caught, each with the message AC2 gives, and `app/` was restored after each.
- AC4: no probe row was left after any of those failing runs, including those that fail the last test. Nor was one
  left after a test made to fail on a statement midway through the last test.
- With one real abandoned checkout in the database, the reconcile file passed and left that row alone.
- AC5: black, isort, flake8 and mypy are clean on `app/`. `pytest -m "not integration"` gave 1,792 passed,
  166 deselected and 9 warnings.
