# Story 1: The five billing integration tests pass against a migrated schema

**Status:** Built on [NosFabrica/brainstorm_server#142](https://github.com/NosFabrica/brainstorm_server/pull/142), at
`a9e531e`. Gate B review pending.
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
- **AC2.** Each test still fails with its own message when the behaviour it covers is broken in `app/`:
  - `update_billing_plan` without `db.refresh(plan)`: the plan-write test fails with `MissingGreenlet`;
  - `record_sync_failure_on_db` restarting `sync_error_since` on every failure;
  - `select_reconcile_candidates_on_db` still sweeping abandoned checkouts;
  - `select_failing_syncs_on_db` listing abandoned checkouts;
  - `select_reconcile_candidates_on_db` re-reading settled subscriptions.
- **AC3.** The probes rely only on what a fresh migration creates: no fixed policy id, and no column the schema
  doesn't have.
- **AC4.** Each test deletes every probe row it could have left before it starts, so a run after a failed one starts
  clean. When a test passes, it leaves none behind.
- **AC5.** The change is test-only: nothing outside the two files changes, and `poe check_all` gives the same result as
  on `staging`.

## Design note

- **Chosen:** every reconcile probe sits on one probe plan (`svc-reconcile-probe` / `plan-reconcile-probe`). The plan is
  made on first use and mapped onto the lowest-numbered existing policy, and the probes that hold a policy hold that
  one. None of the queries under test reads the plan, so which policy it maps to doesn't matter.
- **Chosen:** the plan-write test creates a mapping from the columns a mapping has (service, plan, policy, on sale), and
  its edit takes the mapping off sale. The edit changes a real column, so the UPDATE runs and expires `updated_at`,
  which is the path the test guards.
- **Chosen:** the plan-write test disposes the shared engine in a `finally`, as the reconcile tests already did.
- **Rejected:** creating a policy with id 4 in the tests. It would keep the old numbers, but it ties the probes to an id
  a fresh migration doesn't make, and that id could collide with a real policy on a database that has more.
- **Rejected:** making `billing_plan_id` nullable. The tests would pass, but production always has a plan when it writes
  a subscription (`_grant_and_record`), so the tests would pin a state production never creates.
- **Blast radius:** two test files. The probes write only rows they own (their own pubkeys, service id and plan id), and
  delete them. They read the first `scheduling` row and change nothing in it.

## Out of scope

- Running integration tests in CI.
- The billing code itself, in which these tests found no fault.
- black's layout of the reconcile file. `check_all` lints only `app/`, and `staging` already has that layout.
- The other 161 integration tests.

## Edge cases and regression sentinels

- Within a test, every `_seed` call after the first reuses the probe plan the first one made, instead of inserting a
  second plan.
- A database with no `scheduling` row stops the probe setup with "no scheduling policy to map the probe plan onto",
  rather than with a foreign-key error.
- A database with several policies: the probes use the lowest id, and pass whichever it is.
- Cleanup order: the probe subscriptions are deleted before the probe plan they reference.
- Suite order: the plan-write test runs just before the reconcile file, so a leaked connection shows up there first.

## AC→handle lines

- AC1 → `pytest -m integration` against stores migrated to head; also each file alone, and the pair.
- AC2 → for each behaviour, break it in `app/`, run its test, expect a failure with that test's message, then restore.
- AC3 → `_probe_plan` and `_seed` in the reconcile file; the plan-write test's `_policy_id` and its create call.
- AC4 → each file's `_cleanup`, called at the start of every test (and, in the plan-write test, in `finally` too).
- AC5 → `git diff --stat a896204...a9e531e`; `poe check_all`'s five commands.

## Checks (the implementer's, before review)

Run on 2026-11-12, against throwaway Postgres, Redis, Neo4j and strfry migrated to head. The settings came from
`env.example`, pointed at those stores, less the three Flash keys.

- AC1: 166 passed. Each file alone passed, and so did the pair. Without the fix, 161 passed and these 5 failed.
- AC2: all five behaviours were caught, each with its test's own message, and `app/` was restored after each.
- AC5: black, isort, flake8 and mypy are clean on `app/`. `pytest -m "not integration"` gave 1,792 passed,
  166 deselected and 9 warnings, as on `staging` at `a896204`.
- AC4: not run as a check of its own. The runs above shared one database in sequence and each started clean, but no
  run came straight after a deliberate failure midway through a test.
