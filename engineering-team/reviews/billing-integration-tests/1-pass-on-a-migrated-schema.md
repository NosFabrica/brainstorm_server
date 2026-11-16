# Review: Story 1 — The five billing integration tests pass against a migrated schema

**Reviewer:** Claude (acting as Reviewer, independent of the authoring session)
**Date:** 2026-11-13
**Diff:** `git diff a896204...63bff2b` (`a9e531e` is the fix and PR #142's head; `63bff2b` is the story file, not
pushed yet; the base is `staging` at `a896204`, its tip and the merge-base)
**Lane:** Bug (Light profile, trial)
**Story:** `engineering-team/stories/billing-integration-tests/1-pass-on-a-migrated-schema.md`
**Also read:** PR #142's description (it has no comments or reviews) and both commit messages. Tapestry's harness at
`59d0e7a6d`, by reference: the review template, the Reviewer role, workflows 0 and 5, the story template, the verdict
rule and its ledger entry.

**Method.** Every result below comes from my own runs. None reuses the author's scripts or logs.

- **The gate** is `poe check_all`'s five commands, run by the gate script under `env -i` with the locked tool versions
  (the project venv, and flake8 7.3.0). Each stage's exit code is read from its own line, never through `tail`.
- **The integration runs** use a compose project of my own (`pr142-review`): `postgres:15`, `redis:7`, `neo4j:5` and
  strfry, from images already on the machine (`--pull never`). They're published on spare loopback ports, with
  anonymous volumes and strfry's config inline.
  - Each database was migrated to head (`db78462cf11c`, the only head) with the project venv's alembic. The
    exception is probe H's database, which was migrated at `73ca8bc`.
  - Settings went on the command line under `env -i`: `env.example`, pointed at those stores, less `FLASH_ENABLED`,
    `FLASH_API_KEY` and `FLASH_WEBHOOK_SECRET`. `NOSTR_TRANSFER_FROM_RELAY` names my strfry, `TRUSTED_LIST_RELAY` is
    unset, and `SEARCH_RELAY_URL` names a dead `.invalid` host.
  - No checkout used has a `.env`.
- **Where things ran.**
  - Staging ran in a throwaway checkout at `a896204`.
  - The mutations and the other probes ran in a second throwaway checkout at `63bff2b`, against databases of their
    own. Probe H ran in a third, at `73ca8bc`.
  - The PR's worktree ran only unmodified code, and its `git status` was clean after every run.
- **Versions:** Python 3.12.12, SQLAlchemy 2.0.49, asyncpg 0.29.0, pytest 7.4.4, alembic 1.18.4.

## Quality gates (run by reviewer, not trusted)

| Stage | Branch `63bff2b` | Staging `a896204` |
| --- | --- | --- |
| black `--check ./app` | exit 0, 201 files unchanged | exit 0, 201 files unchanged |
| isort `--check ./app` | exit 0 | exit 0 |
| flake8 `./app` | exit 0 | exit 0 |
| mypy `./app` | exit 0, no issues in 201 source files | exit 0, no issues in 201 source files |
| pytest `./tests`, integration deselected | exit 0: 1,792 passed, 166 deselected, 9 warnings | exit 0: 1,792 passed, 166 deselected, 9 warnings |

Neither run shows a skip or xfail marker. The 9 warnings are the same on both trees, all in untouched fast-suite files
(`test_alembic_heads.py`, `test_auth_challenge.py`, `test_billing_repo.py`). That's exactly the 2026-11-12 staging
baseline.

**The integration runs, which CI never makes.**

| Run | Tree | Result |
| --- | --- | --- |
| `pytest -m integration ./tests` | staging | exit 1: **5 failed, 161 passed** (1,792 deselected, 4 warnings). The five are the billing tests. The plan-write test raises `TypeError: insert_billing_plan_on_db() got an unexpected keyword argument 'amount_minor'` (`app/services/billing_service.py:511`). The first reconcile test, the error clock, raises `RuntimeError: Event loop is closed`. The other three raise `NotNullViolationError` on `user_subscription.billing_plan_id` |
| the same | branch | exit 0: **166 passed** (1,792 deselected, 3 warnings). The log shows no pool error, and no billing row is left behind |
| the plan-write file alone; the reconcile file alone; the pair; the pair reversed | branch | exit 0 each: 1, 4, 5 and 5 passed |
| the reconcile file alone | staging | exit 1: all four fail with the NOT NULL violation, the error clock included |

Staging's fourth warning is an un-awaited asyncpg `Connection._cancel` coroutine, reported in the first test after the
billing files. It's left over from the leaked pool, and the branch doesn't have it.

**The two test files, which `check_all` doesn't lint.**

- isort and flake8 exit 0 on both trees.
- black exits 1 on both, for the reconcile file only, with six hunks each.
- Of the 42 non-blank lines the PR adds to that file, black keeps 41 verbatim. The exception is
  `db, SETTLED_GRANTED, status="expired", granted=True,`, a one-token change inside a call whose layout predates the
  PR.
- black keeps all 33 lines the PR adds to the plan-write file.

## Diff walk

- **`tests/integration/test_billing_plan_write_integration.py`.**
  - The create drops `amount_minor` and `currency` (`:57–65`), and the edit becomes `{"is_active": False}` (`:75–79`).
  - The body now sits in an outer `try`, whose `finally` disposes the shared engine in-loop (`:82–85`). That's the
    pattern `test_support_diagnostics_expiry_integration.py:74–79` and `test_reap_stale_runs_integration.py:53`
    already use.
- **`tests/integration/test_billing_reconcile_candidates_integration.py`.**
  - `_probe_plan` (`:72–93`) finds or makes one probe plan, mapped onto
    `SELECT id FROM scheduling ORDER BY id LIMIT 1`.
  - `_seed` (`:96–122`) puts every probe on it. `granted` becomes a flag that grants the plan's policy (`:115`), and
    the two granting calls change to match (`:198`, `:328`).
  - `_cleanup` (`:137–147`) deletes the probe plan after the subscriptions.
  - The file now imports `get_billing_plan_on_db` and `insert_billing_plan_on_db`.
- **The story file** is new and documentation only.

`git diff --stat a896204 a9e531e` lists the two test files and nothing else. Nothing under `app/` or `alembic/`
changes, and neither does `pyproject.toml` or `poetry.lock`.

## Spec adherence: AC verdict table

This is a test-deliverable story: the deliverable is the repaired tests. Tapestry's guard-suite carve-out
(`templates/adr.md`) assumes a Tester, and the Bug lane has none. So this review's mutation runs (M and T, under
Adversarial probes) are the repaired tests' independent judge.

| AC | Evidence | Verdict |
| --- | --- | --- |
| **AC1:** the five pass alone, together, and in the full run | The branch runs above: 166 of 166; each file alone; the pair; the pair reversed | **Met** |
| **AC2:** each test fails with its own message when its behaviour breaks | My own edits, M1–M5, in a throwaway checkout. M1: `MissingGreenlet` (plan-write). M2: "a repeat restarted the clock". M3: "abandoned checkout still swept". M4: "abandoned checkout crowds the failures". M5: "still re-reading a settled subscription". Each time, the other four passed | **Met** |
| **AC3:** the probes use only what a fresh migration creates | A fresh migration seeds one policy (`1`, `Weekly`). The probes take the lowest id and name no dropped column. They pass on a database whose lowest policy is `0`, disabled and private, and the probe plan lands on `0`. With no policy at all, setup stops with its own assertion messages, not with a foreign-key error | **Met** |
| **AC4:** each test cleans up first, and a pass leaves nothing | `_cleanup` opens every test. After a failure midway through the file's last test (M5, M7, M9, M10), five probe subscriptions and the probe plan remain. The next unmodified run passes 5 of 5 and leaves nothing. A plan-write failure (M1) leaves nothing, thanks to its `finally` | **Met.** The leftover isn't inert, though (Non-blocking 1) |
| **AC5:** test-only, and `check_all` matches staging | The diff stat and the gate table above | **Met.** The branch also adds the story file, a process artifact |

**Are these the right ACs for this bug?** Mostly.

The bug is drift of three kinds: a test calling code that has changed, seeds the schema refuses, and a cross-test
engine leak. AC1, AC3 and AC5 cover the repair. AC2 matters most, since the five had never been green on this schema:
it's what shows they aren't vacuous. I see two gaps:

- **AC2 doesn't pin the diff's own rewrite.** It names one behaviour per test, and none of the five touches the
  rewritten granting probes (`granted=True`). My M8 and M9 do, and both are caught (Non-blocking 3).
- **No AC says probe rows are inert outside the tests.** The Design note's blast radius asserts it, and for the probe
  plan it doesn't hold (Non-blocking 1).

AC2's first bullet is rightly confined to the edit: the create half of that test can't catch a missing refresh
(Non-blocking 2).

## Claims adherence

These are the claims no AC covers: in the PR description, the commit messages, the story, and the new code comments.

| # | Claim | Where | Evidence | Verdict |
| --- | --- | --- | --- | --- |
| C1 | CI deselects the `integration` marker (`.github/workflows/build.yml:58`) | PR, commit, story | Line 58 is `PYTEST_ADDOPTS: "-m 'not integration'"` | Holds |
| C2 | `a8d0e40` dropped `amount_minor` and `currency` from `billing_plan` when pricing moved to Flash | PR, commit, story | `a8d0e40` added a migration that drops eight columns, those two and `includes` among them, and took them out of `insert_billing_plan_on_db`'s signature. `88721bb` folded that migration into the squashed billing schema, so a fresh migration never creates them (checked on a migrated database) | Holds |
| C3 | The old edit set `includes`, so even with the create fixed it no longer issued the UPDATE that reproduces the `MissingGreenlet` | PR, commit, story | Probe T2: staging's test, with only the two create columns removed, passes even without `update_billing_plan`'s refresh | Holds |
| C4 | A NULL `billing_plan_id`, which "the schema has never allowed" | PR, commit, story | It's NOT NULL at `7ea0859`, which introduced it; at `8ddcebd`, which wrote the first of these tests; at the squashes `f31fada` and `679f32c`; and at head. No line added in any ref's history declares it nullable | Holds |
| C5 | A fresh migration creates no policy 4; it seeds only `Weekly` | PR, commit, story | `f3a9c1b2d4e5` seeds one row, and a migrated database holds only `1: Weekly`. Probe T4 keeps the plan fix but puts the two granting probes back on policy 4, and both tests fail on `granted_scheduling_id`'s foreign key. So policy 4 was a cause in its own right | Holds |
| C6 | Every probe sits on one probe plan. The probes that hold a policy hold that one. The cleanup deletes the plan after the subscriptions | PR, commit, story | `:72–147`; M8 and M9 are caught; cleanup succeeds over leftovers | Holds |
| C7 | The leaked pool made the first reconcile test fail with "Event loop is closed", "though it passed alone" | PR (the story says "instead") | Staging's full run shows the failure. Probe T1 (the branch without the new dispose) reproduces it, and the restored test passes. But on staging that test also fails alone, with the NOT NULL violation. It passes alone only once the seeding is fixed | Partly: the wording (Non-blocking 5) |
| C8 | The reconcile tests already disposed the engine | PR, commit | On staging, all four end in `finally: await engine.dispose()` | Holds |
| C9 | The PR comes from a fork, so it gets no CI | PR | GitHub: `isCrossRepository` is true and `statusCheckRollup` is empty. `build.yml` triggers only on `push` and `workflow_dispatch` | Holds |
| C10 | The branch is `staging` at `a896204` plus the fix | PR | `git log`. GitHub: `baseRefOid` `a896204`, `headRefOid` `a9e531e`, mergeable, state `CLEAN` | Holds (the story commit is local) |
| C11 | 166 pass with the fix; without it, 161 pass and these 5 fail | PR, commit, story | My two full runs | Holds |
| C12 | "None of the five had passed before" | PR | The four reconcile tests never could have: from their first commit, their seeds put NULL in a NOT NULL column. The plan-write test, though, passed at its own commit `73ca8bc`, against a database migrated there, and caught a missing refresh with `MissingGreenlet` (probe H). It broke when `a8d0e40` changed `insert_billing_plan_on_db`'s signature, about 15 hours later | Partly (Non-blocking 5) |
| C13 | The four linters are clean on `app/`, and pytest gives 1,792 passed and 166 deselected, as on staging | PR, story | The gate table | Holds |
| C14 | On the two test files, isort and flake8 are clean, and black's only complaints are the reconcile file's existing layout, as on staging | PR, story (Out of scope) | The lint runs above | Holds |
| C15 | #120's Gate B re-review found the five (161 of 166), and with this commit #120's steps give 166 | PR, story | `engineering-team/reviews/memory-sweep/server-docs-2026-10-29.md:605` at `15bde16`, in its re-review's "Checks run": "Run B: the README's steps … 161 passed, 5 failed". My branch run follows those steps | Holds |
| C16 | The plan-write test came with `73ca8bc`, and the four reconcile tests with `8ddcebd`, `168212d`, `06bb99b` and `320c167` | Story | `git log`. `8ddcebd` added two of the four, and `168212d` and `320c167` one each. `06bb99b` added none: it put the Python/SQL cross-check into an existing test | Holds collectively |
| C17 | None of the queries under test reads the plan | Story (Design note); code comment `:67` | The five repo functions read only `user_subscription` columns, and a probe plan on a disabled, private policy changes nothing | Holds |
| C18 | The one path that writes a subscription, `_grant_and_record`, always has a plan | Story; code comment `:65–66` | The only insert is `upsert_user_subscription_on_db`, called only at `billing_service.py:472`, with `plan.id` | Holds |
| C19 | Blast radius: "The probes write only rows they own … and delete them. They read the first `scheduling` row and change nothing in it" | Story `:74–75` | The rows hold. The effect doesn't: while it exists, an on-sale probe mapping changes what `/billing/plans` answers | Partly (Non-blocking 1) |
| C20 | The edge cases: plan reuse within a test, no policy, several policies, cleanup order, suite order | Story `:86–92` | Each is checked. Every leftover has one plan row. I ran the no-policy and several-policy databases, and cleanup over leftovers. In collection order the billing files are tests 1–5 of 166 | Holds |
| C21 | "unrefreshed, this raises MissingGreenlet", over the create's assertions | Code comment `:66–67`, which predates the PR and was only re-indented | M1b and the expiry probe: it's false for the create | Doesn't hold (Non-blocking 2) |
| C22 | The author's environment ("the one-click deployment's images, on spare ports") and the author's own AC2 edits | PR, story | Not reproducible from sources | Unverified; every result was reproduced independently |

## Evidence table

| Source or command | What it showed |
| --- | --- |
| The gate script on both trees | All five stages exit 0 on both, with 1,792 passed, 166 deselected, 9 warnings and no skips |
| `pytest -m integration ./tests` on both trees, against my migrated stores | Staging: 5 failed, 161 passed (exit 1). Branch: 166 passed (exit 0) |
| The billing files alone, paired and reversed; staging's reconcile file alone | Branch: 1, 4, 5 and 5 passed. Staging: four NOT NULL failures |
| black, isort and flake8 on the two test files, on both trees; black over copies of the branch files, against the PR's added lines | isort and flake8 exit 0. black exits 1 on the reconcile file only. Black-clean: 41 of 42 added lines in that file, 33 of 33 in the plan-write file |
| `information_schema` and `scheduling` on a fresh migration | One head, `db78462cf11c`. One policy, `1: Weekly`, default and public. `billing_plan`'s columns are `id`, `flash_service_id`, `flash_plan_id`, `scheduling_id`, `is_active`, `created_at`, `updated_at`. `billing_plan_id` is NOT NULL |
| `git show` and `git log` on `a8d0e40`, `88721bb`, `679f32c`, `7ea0859`, `8ddcebd`, `f31fada`, `73ca8bc`, `168212d`, `06bb99b` and `320c167`; `git log --all -p` over the models and migrations | C2, C4 and C16 |
| `alembic/versions/f3a9c1b2d4e5_add_scheduling_table.py:32–43` | The only `scheduling` seed: `Weekly` |
| `.github/workflows/build.yml` | `on:` `push` (all branches, `v*` tags) and `workflow_dispatch`; line 58 deselects the marker |
| The mutations M1–M12 and the premise probes T1–T4 and H, in throwaway checkouts | The table under "Do the tests catch what they cover?" |
| `list_billing_plans` (billing on, with the Flash mock, so no network): with the leftover probe plan, after the next run, and with the plan off sale | `[]`; then the `Weekly` free row; then the `Weekly` free row |
| An INSERT flush and an UPDATE flush of a `BillingPlan`, then a rollback | `eager_defaults` is `auto`. After the INSERT nothing is unloaded; after the UPDATE, `updated_at` is |
| Databases with policies 0–4, with none, and with one real abandoned checkout | 5 passed, with the probe plan on policy 0. 5 failed at setup, with the setup's own messages. The alarming-sections test fails: "the abandoned section disagrees with the sweep" |
| The database unreachable | 5 failed, each with `ConnectionRefusedError`, in under a second in all, with no knock-on failure |
| `pytest --collect-only -m integration` | 166 tests, all in `tests/integration/`. The billing files are tests 1–5, and 13 later tests in six files import the shared engine |
| The one-click compose file and `env.example` | The stack's server reads its own `brainstorm-database`. `env.example`'s `DB_URL` names no database, so the tests use the default one |
| GitHub, read-only: three calls to `gh pr view 142` | The description, no comments or reviews, and the checks, fork, merge state and refs in C9–C10 |
| #120's branch at `15bde16`: its review, and `README.md:110–115` | C15, and the README paragraph this PR makes stale (Non-blocking 4) |

## Design-note adherence (in place of "ADR adherence")

- [x] **The files match the note:** the two test files, with the three "Chosen" bullets as written.
- [x] **The rejected alternatives are real ones.** A fixed policy 4 could collide on a database with more policies. A
  nullable `billing_plan_id` would be a schema change (an irreversibility trigger), and would pin a state production
  never writes.
- [x] **No new dependency:** `pyproject.toml` and `poetry.lock` are unchanged.
- [ ] **The blast radius is complete:** not quite (Non-blocking 1).

## Gate A classification: ratified

The story classifies itself as Bug lane with a Design note, and that's right. The existing behaviour that's wrong is
five tests failing against the current schema and code, whatever the setup, and the fix touches nothing else.

| Trigger | Present? | Why |
| --- | --- | --- |
| A wire format or event shape | No | No event is built or signed |
| An auth/trust default | No | |
| A schema or migration | No | Nothing under `alembic/` or `app/db_models/` changes; the tests seed rows into the existing schema |
| A new dependency | No | The manifest and lock file are unchanged |
| A cross-repo contract | No | The probe ids live only in these two files. #120's README cites #142 in prose (Non-blocking 4), which is no contract |
| Request routing or middleware ordering | No | |
| Response headers or content-type | No | |
| A value in more than one repo | No | |

## Repo-conventions integrity (in place of "Concept-graph integrity")

Tapestry's concept-graph and firmware checks don't apply: this repo has neither. These are its equivalents:

- [x] **Async.** Each test drives one `asyncio.run`, as before.
- [x] **The engine is disposed in-loop.** The plan-write test now matches the reconcile tests and
  `test_support_diagnostics_expiry_integration.py:74–79`.
- [x] **The client tag:** not applicable, since nothing is signed.
- [x] **Loggers:** not applicable, since no logging is added.

## Adversarial probes (beyond the plan)

**Premise errors.** Each stated cause is a cause in its own right:

- the `TypeError` and the NOT NULL violation, by staging's run;
- policy 4, by T4;
- the leaked pool, by T1.

The vacuity premise holds too (T2). Only the PR description's "None of the five had passed before" reaches too far:
the plan-write test was green at its own commit (probe H; Non-blocking 5).

**Do the tests catch what they cover?** For each row I ran both billing files from a throwaway checkout, against a
database of its own, and restored the checkout afterwards. Unless a row says otherwise, the other tests passed.

| Id | Broken | Result |
| --- | --- | --- |
| M1 | `update_billing_plan` without its `db.refresh(plan)` | plan-write: `MissingGreenlet` |
| M2 | `record_sync_failure_on_db` sets `sync_error_since=now` on every failure | error clock: "a repeat restarted the clock" |
| M3 | `select_reconcile_candidates_on_db` without its abandoned exclusion | abandoned-drops-out: "abandoned checkout still swept" |
| M4 | `select_failing_syncs_on_db` without its abandoned exclusion | alarming sections: "abandoned checkout crowds the failures" |
| M5 | `select_reconcile_candidates_on_db` without its settled exclusion | settled: "still re-reading a settled subscription" |
| M1b | `create_billing_plan` without its refresh | **Not caught:** 5 passed (Non-blocking 2) |
| M2b | the clock never restarts (`coalesce(sync_error_since, now)`) | error clock: "a new reason kept the old clock" |
| M6 | `select_stale_syncs_on_db` without its abandoned exclusion | alarming sections: "abandoned checkout leaked into stale syncs" |
| M7 | `select_stale_syncs_on_db` without its settled exclusion | settled: "settled subscription leaked into stale syncs" |
| M8 | `AbandonRule.condition` and `.matches` ignore the grant | abandoned-drops-out: "stopped watching a subscriber with a policy" |
| M9 | `settled_condition` ignores the grant | settled: "stopped watching a live tier" |
| M10 | `settled_condition` ignores an open error | settled: "settled a row with an open error" |
| M11 | `AbandonRule` ignores its window | abandoned-drops-out: "dropped a checkout still in its window" |
| M12 | `AbandonRule` ignores which error it is | two tests: "an outage was treated as abandonment" and "a real failure was hidden" |
| T1 | the branch's plan-write test without its new `engine.dispose()` | error clock: `RuntimeError: Event loop is closed` |
| T2 | staging's plan-write test minus only `amount_minor` and `currency`, keeping its `includes` edit, together with M1 | The plan-write test **still passes**: the old edit never reaches the UPDATE. The error clock fails as in T1, since the old test has no dispose |
| T3 | T2 without M1 | The plan-write test passes; the error clock fails as in T1 |
| T4 | the branch's reconcile file with the two granting probes back on policy 4 | The two granting tests fail on `granted_scheduling_id`'s foreign key |
| H | `73ca8bc`, the plan-write test's own commit, on a database migrated there; then the same with M1 | Passes; then `MissingGreenlet` |

Every exclusion rule the four reconcile tests pin was broken once and caught, both rewritten granting probes included.
I didn't target every positive assertion (`PAUSED`, `CANCELED`, the Python/SQL cross-check on its own). The plan-write
test's edit half is live, and its create half is vacuous (M1b).

**Collateral damage.**

- **The rows.** The probes write and delete only rows they own: ten probe pubkeys, and the service ids
  `svc-write-probe` and `svc-reconcile-probe`. `create_billing_plan`'s `reset_events_awaiting_plan_on_db` touches only
  webhook events that name the probe's own ids. `scheduling` is only read.
- **The effect.** An on-sale mapping hides its policy's free row from `/billing/plans`
  (`app/services/subscription_view_service.py:339–345`). The probe plan is on sale, on the lowest policy, and survives
  a failed test (Non-blocking 1).
- **The disposal.** It costs the later tests nothing. All 161 pass, 13 of them on the shared engine, and the
  leaked-pool warning is gone.

**Other databases.**

- **More policies** (0–4, the lowest disabled and private): 5 passed, with the probe plan on policy 0.
- **No policy:** all five stop at setup, with "no scheduling policy to map a plan onto" and "…the probe plan onto", and
  leave nothing behind.
- **Rows already there:** one real abandoned checkout fails the alarming-sections test. Its equality assertion predates
  the PR (Non-blocking 6).

**Leftovers.** A failure midway through any of the first three reconcile tests leaves nothing, because the next test
cleans up first. A failure in the last one leaves five probe subscriptions and the on-sale probe plan until the next
run, which removes them and passes.

**Error paths and order.**

- **The database unreachable:** five fast `ConnectionRefusedError`s, with no hang and nothing masked.
- **The order.** Reversed, the pair still passes. One dependence remains: the reconcile tests dispose only at the end,
  so they rely on whatever ran before them disposing. Disposing at the start as well, as
  `test_reap_stale_runs_integration.py:41` does, would make the file immune. Every user of the shared engine now
  disposes in a `finally`, so nothing leaks today. That's an option, not a finding.

## Things tests can't catch

- [x] **No secrets.** The probe ids are made up.
- [x] **No debug output, no commented-out code, no TODO.**
- [ ] **Error paths and edge cases are handled where it matters.** Not quite: a failed test leaves an on-sale mapping
  behind (Non-blocking 1).
- [x] **Concurrency.** Two runs at once against one database would collide on the probe ids. That predates the PR, and
  nothing runs them in parallel.
- [x] **Security:** not applicable to a test-only change.

## House rules check

- [x] No new lint, typecheck or build tooling, and no config change.

Product-guide adherence doesn't apply: there's no PRD, and nothing is user-facing. Neither does Playwright.

## Findings

### Blocking

None.

### Non-blocking

1. **`tests/integration/test_billing_reconcile_candidates_integration.py:87–92`, with `_cleanup` reached only on success
   (`:177`, `:239`, `:301`, `:377`): the probe plan is created on sale, so a leftover can empty the public plan list.**
   - **What happens.** `insert_billing_plan_on_db` defaults to `is_active=True` (`app/repos/billing_plan_repo.py:59`),
     and the probe plan maps onto the lowest policy. While an on-sale mapping points at a public policy,
     `list_billing_plans` drops that policy's free row (`app/services/subscription_view_service.py:339–345`). On a
     fresh migration that policy is `Weekly`, the default and the only public one, so `/billing/plans` answers `[]`.
   - **The probe.** With billing on and the Flash mock, the list was `[]` with M5's leftover. It held the `Weekly` row
     after the next run, and with the probe plan off sale.
   - **The reach.** It needs `DB_URL` to name a database that a server with billing on reads. With `env.example`'s
     `DB_URL` the tests use the default database, not the one-click server's own, so the README's steps are safe.
     After a failure the effect lasts until the next run; during a run, for milliseconds.
   - **Why it isn't blocking.** It's that narrow, and re-running clears it. The plan-write test's on-sale mapping has
     the same window during its own run, but cleans up in a `finally`.
   - **Optional improvement.** Run each reconcile test's `_cleanup` in a `finally`, as the plan-write test does. Or
     create the probe plan off sale, since nothing under test reads it; leftover probe subscriptions would then appear
     in the admin report's retired-plan section instead. Either way, amend the Design note's blast radius. It's a cheap
     change, worth taking before merge.
2. **`tests/integration/test_billing_plan_write_integration.py:66–69`: the comment says an unrefreshed create raises
   `MissingGreenlet`. It doesn't.**
   - **Why.** INSERT server defaults come back through RETURNING (`eager_defaults` is `auto`). After the INSERT flush
     nothing is unloaded; after the UPDATE flush, `updated_at` is.
   - **What that means.** M1b removed `create_billing_plan`'s refresh, and all five still passed. The edit half is the
     whole guard, which is all AC2 claims.
   - **Not new.** The comment predates the PR, which only re-indented it.
   - **Optional improvement.** Say that only the edit reproduces the bug, or move the comment to the edit's assertions.
3. **Story AC2 and its AC→handle line (`1-pass-on-a-migrated-schema.md:48–53` and `:97`): no listed behaviour pins the
   diff's own rewrite.**
   - M8 (`AbandonRule` ignoring the grant) and M9 (`settled_condition` ignoring it) are caught through the
     `granted=True` probes, so the rewrite works. The record should say so.
   - **Optional improvement.** Add both to AC2's list, and their handle to `:97`.
4. **Across PRs: #120's README (`README.md:110–115` at `15bde16`) says the five "fail whatever the setup until #142".**
   - Once both have landed, that paragraph is stale, and neither PR says who removes it.
   - **Suggested follow-up.** Remove it in whichever PR merges second, and note that in this story's Out of scope.
5. **PR #142's description (not committed): two sentences claim more than the evidence shows.**
   - "though it passed alone": on staging, the error-clock test fails alone too, with the NOT NULL violation.
   - "None of the five had passed before": the plan-write test passed at `73ca8bc` (probe H).
   - The story and the commit messages are accurate on both points.
   - **Optional improvement.** Tighten both sentences if the description is edited.
6. **`test_billing_reconcile_candidates_integration.py:299`, older than the PR but reachable now that the tests run: an
   assertion over the whole table.**
   - It asserts `counted == {ABANDONED}` over every row, and every query is capped at 500. So on a database that
     already holds an abandoned checkout, the test fails; one real row was enough. AC3 covers what the probes need,
     not what else may be there.
   - **Optional improvement.** Compare the probes only (`counted & set(ALL_PROBES) == {ABANDONED}`), or say in the
     module docstring that the tests want a database with no other billing rows.

### Harness friction

*(Each becomes an `engineering-team/OPEN.md` row, type `meta`.)*

1. **The Bug lane has no stand-in for tapestry's guard suite on a test-deliverable story.**
   - The implementer judged the repaired tests with mutations that exist only in their own session. The story says
     "all five behaviours were caught" without recording the edits, so this review re-derived them.
   - **Suggested.** Ask such a story to record each mutation in one line (function, edit, expected message), and name
     Gate B's re-run as the independent judge.
2. **The profile doesn't say when a Bug-lane story is written.**
   - `63bff2b`'s message says "Written after the build, as the Light profile's Bug lane asks", but the profile doesn't
     address order.
   - Written afterwards, the ACs were fitted to the fix: AC2 lists exactly the author's five mutations. This review
     checked the fit.
   - **Suggested.** Say whether a post-build story's ACs get that check at Gate B.
3. **The template still needs adapting by hand.**
   - Its npm gate lines and its concept-graph section are re-adapted every time.
   - It still closes with the section the verdict rule reads as a verdict (tapestry ledger
     `2026-10-07-on-pass-heading-reads-as-verdict`). This file names that section "Close-out".
   - Both are known, and recorded here for the count.
4. **The brief's `gh pr view 142 --comments` printed nothing off a TTY, and exited 0.**
   - It cost one of the review's capped GitHub calls; `--json body,comments` worked.
   - **Suggested.** Briefs should name the JSON form.

## Verdict

**Recommendation:** PASS (mergeable as-is)

The PR does what it says:

- The five tests pass on a schema migrated to head: alone, together, and in the full suite (166 of 166).
- On staging they fail exactly as the PR describes.
- Each behaviour the story names is caught, with that test's own message. Eight of my nine further mutations are caught
  as well, the diff's rewritten probes among them.
- Each stated cause holds as a cause in its own right.
- The change is test-only, and the gate is identical to staging's.
- The Bug-lane classification is ratified.

Nothing blocks. Non-blocking 1 is a `finally` or one argument, and it's the one worth taking before merge.

## Close-out (the template's next step)

If the operator's verdict agrees, the template's On PASS step asks for three things in this review's own commit:

- the story's Status line ("Built on #142, at `a9e531e`. Gate B review pending.") flipped to Done, in place;
- tapestry's message form for that commit, `review: <slug> — <verdict> (<epic> #<n>)`, here epic
  `billing-integration-tests`, story 1;
- the push, on top of the story commit `63bff2b`, which isn't on the PR yet.

Then comes completion detection. The Light profile keeps books for multi-story efforts, and no
`engineering-team/audits/*/book.md` names this story, so there's no book to offer to close. That result belongs in the
chat, not here.

Tapestry's epic close-out would move this epic's story and review folders under `done/` once it ships. This repo hasn't
used `done/` so far: shortest-path's story is Done in place.

Under the Light profile the operator gives the verdict, so this file is uncommitted and changes nothing else.

**Operator's verdict (Gate B):** see "Operator verdict (2026-11-13)" at the end of this file.

## Operator verdict (2026-11-13)

**PASS**, given in the session as "Fix, then pass": take the review's six non-blocking findings first, then pass. The
authoring session took all six in `c836b55`, re-ran the checks, and recorded the verdict here on the operator's word.

- **Non-blocking 1.** Each reconcile test now ends in a `finally` that calls `_finish`, which removes every probe row
  in a fresh session and then disposes the engine. The story's Design note, AC4 and edge cases say so.
- **Non-blocking 2.** The plan-write test's comments say that only the edit reproduces the `MissingGreenlet`, and that
  after an INSERT the server's defaults come back with the row.
- **Non-blocking 3.** Story AC2 lists all seven edits with the message each should produce, the two grant checks
  included, and its handle line names them.
- **Non-blocking 4.** The story's Out of scope says that whichever of #120 and #142 merges second removes #120's
  "until #142" paragraph.
- **Non-blocking 5.** The two sentences in the PR description are corrected. That text isn't committed.
- **Non-blocking 6.** The abandoned-section check compares only the probes.

The checks were re-run on `c836b55`, against a fresh throwaway stack migrated to head:
- 166 of 166 passed, both before and after the checks below, on the same database. Each file alone, the pair, and the
  pair in reverse order passed.
- All seven AC2 edits were caught, each with its message, and so was a test made to fail on a statement midway through
  the last test. After every one of those failing runs, no probe row was left.
- With one real abandoned checkout in the database, the reconcile file passed and left that row alone.
- The gate's five stages all exit 0, with 1,792 passed, 166 deselected and 9 warnings.

The fixes weren't re-reviewed: the operator passed the PR with them in. Not taken: the four harness-friction items,
which stay listed above. Apart from its last line, the review above is as its reviewer wrote it, and it's committed with
this verdict. Its closing "this file is uncommitted" was written before that commit.
