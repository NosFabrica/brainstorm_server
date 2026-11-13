# Review (doc lane): server docs from the 2026-10-29 memory sweep, PR #120

**Reviewer:** Claude (acting as Reviewer, independent of the authoring session)
**Date:** 2026-11-12
**Diff:** `git diff a896204...6d0b17d` (head `6d0b17d` on `docs/sweep-david`, from the fork nousclawds4/brainstorm_server;
base `staging` at `a896204`, which is also the merge-base)
**Lane:** Doc / one-liner (Light profile, trial), docs-mode
**PR:** [NosFabrica/brainstorm_server#120](https://github.com/NosFabrica/brainstorm_server/pull/120), "docs: the local
stack is one-click's; what production runs; two OPEN rows". Its description and its three comments (wds4, en-nostr,
nousclawds4) are in scope.
**Story file:** none. In the doc lane, this review is the record (tapestry `workflows/0-intake.md` §3, line 31).
**Rubric:** this repo's `engineering-team/workflows/light-profile.md` (Lanes, the irreversibility triggers, § Gate B).
Tapestry at `59d0e7a6d`: `templates/review-checklist.md` with its docs-mode variant, `roles/reviewer.md`,
`workflows/5-review.md`, `workflows/0-intake.md`, and `workflows/protocol-spec-workflow.md` § Docs-mode rules. The
adaptation follows this repo's previous Gate B review (`f0a856f`,
`reviews/assistant-dictionary/1-assistant-copies-concepts.md`).
**Profile:** Light (trial), Gate B at full rigor. Docs get full-depth prose.

**Commits audited:**

- `5ee118e`, `c1bee18` and `4973df4` (2026-10-29) sit on `main` at `6805f51` (#107, 2026-10-26), which `a896204`
  already contains.
- `74cacae` (2026-11-12) merges `staging` at `a896204`.
- `6d0b17d` (2026-11-12) answers en-nostr's comment.

The three-dot diff is five Markdown files, +67/−10: `BIBLE.md`, `CLAUDE.md`, `README.md`, `engineering-team/OPEN.md` and
`engineering-team/workflows/light-profile.md`.

**Independence:** I wrote the claims table from the files, commands, git history, the live GETs and GitHub reads listed
below. The PR description and thread are the only author-side text I read. The authoring session's scratch files share
this review's scratch directory, and I didn't open them (Harness friction 2).

## Quality gates (run by reviewer, not trusted)

### Doc-facing checks

| Check | How | Result |
| --- | --- | --- |
| Links in the added text resolve | `gh api graphql` for the one-click repo; `gh issue view 44`; `curl -I` of the tapestry link; `curl` GETs of both `openapi.json` URLs | All resolve. One-click is public. #44 exists. Both `openapi.json` answer 200 JSON. The tapestry link resolves only by a 301 to `TapestryProtocol/tapestry` (Non-blocking 1) |
| Anchors and cross-references | `[Run locally](#run-locally)` against `CLAUDE.md:83`; "README § How to run the tests" against `README.md:43`, whose note at 74–77 has the 7688 port; "CLAUDE.md § Run locally"; `app/services/CLAUDE.md` → Errors (`### Errors`, line 52); every file and function the OPEN row names | All accurate |
| Index and map rows | The directory-map row for `docker-compose.yml` (`CLAUDE.md:63`); OPEN.md's two rows, checked with `awk` | The row is updated. The OPEN rows are dated and in date order, and every row has 4 cells |
| Markdown structure | Code fences counted | Balanced: 4 in CLAUDE.md, 24 in README.md |
| Links next to the change (not changed by it) | `gh api` on `NosFabrica/brainstorm-k8s` | 404 for the account this review ran as (nousclawds4). So `CLAUDE.md:119` and `README.md:102–105` can't be followed from it (Harness friction 3) |

### Code gate (no regression)

This repo's gate is `poe check_all`. I ran its five stages with
`gate.sh <worktree>`, from the worktree, under `env -i`. There's no `.env` in
the worktree, and `PYTEST_ADDOPTS="-m 'not integration'"` is set as CI sets it. Output went to a file, and I read each
stage's `== exit N` line. Nothing was piped through `tail`. The tool versions are those in `poetry.lock`: Python 3.12.12,
black 23.12.1, isort 5.13.2, flake8 7.3.0 (from a scratch venv), mypy 1.20.1 and pytest 7.4.4.

| Stage | Command (poe task) | Result |
| --- | --- | --- |
| check_fmt | `black --check ./app` | exit 0; 201 files would be left unchanged |
| check_imports | `isort --check ./app` | exit 0 |
| check_lint | `flake8 ./app --max-line-length 100 --extend-ignore E203` | exit 0 |
| check_types | `mypy ./app` | exit 0; "Success: no issues found in 201 source files" (plus the six `annotation-unchecked` notes in `nostr_event_transferer.py`, which were there before) |
| test | `pytest ./tests -q -p no:cacheprovider` | exit 0; **1792 passed, 166 deselected, 9 warnings in 12.26s**. The progress lines are all dots, with no skip, xfail or error markers. The warnings are the known ones in untouched suites: JWT key length, alembic `path_separator`, un-awaited `AsyncMock`s |

**The result matches the given staging baseline exactly** (1,792 passed, 166 deselected, 9 warnings). I didn't re-run
on `a896204`. The git tree hashes of `app/`, `tests/`, `alembic/`, `scripts/`, `pyproject.toml` and `poetry.lock` are
identical at `a896204` and `6d0b17d` (`git rev-parse <rev>:<path>`), and the two-dot diff names only the five Markdown
files. So the code gate can't differ from the base.

I didn't run the integration tests. They need the stack, and starting containers is outside this review's bounds. The
README's integration claims are checked by reading the tests (Blocking 1).

## Diff walk (file by file)

- **`BIBLE.md:89`** (§8) and **`engineering-team/workflows/light-profile.md:3`**: tapestry's harness link changes from
  `nous-clawds4/tapestry` to `nousclawds4/tapestry`. Nothing else changes on either line.
- **`CLAUDE.md`**
  - The directory-map row (`:63`) now says `docker-compose.yml` holds only `web` and Postgres.
  - § Run locally (`:85–112`) names one-click's compose plus its local override as the local stack, with the command.
    It adds a dated paragraph on how far one-click lags, explains why this repo's compose is refused, and gives a
    recipe for running the server on the host.
  - A new paragraph under § Deploying to staging (`:121–131`) covers how production takes images. It gives a dated
    observation of missing routes and a way to compare route lists.
- **`README.md`**
  - The integration tests now need "Postgres, Neo4j and Redis" (`:46–47`).
  - A note covers `PUBLIC_BASE_URL` and the fast suite (`:55–60`).
  - The integration command moves to one-click and starts three services (`:62–72`).
  - "How to run everything with docker" points at one-click (`:85–97`).
- **`engineering-team/OPEN.md`**
  - The error-envelope row (`:9`) gets its issue (#44) and a corrected doc pointer (`app/routers/CLAUDE.md` →
    `app/schemas/CLAUDE.md`).
  - A new row (`:14`) records ORE-06/07's `total` against `results`.

## Claims-adherence table

Every verdict comes from the evidence named. "Holds" means the evidence confirms it; "partly" means part of it is true;
"wrong" means the evidence contradicts it; "unverified" means I couldn't check it, for the reason given.

### CLAUDE.md

| # | Claim | Where | Evidence | Verdict |
| --- | --- | --- | --- | --- |
| C1 | This repo's `docker-compose.yml` defines only the server (`web`) and Postgres, not the local stack | `CLAUDE.md:63`, `:102–104`; `README.md:63–64`; PR body | Read: two services, `web` and `brainstorm-database` | **Holds** |
| C2 | `web` depends on a `brainstorm-neo4j` it doesn't define, so compose refuses the file (the body says "`docker compose config` rejects it") | `CLAUDE.md:103–104`; PR body | `docker compose -f docker-compose.yml config` (Compose 5.1.0): exit 1, `service "web" depends on undefined service "brainstorm-neo4j": invalid compose project`. `git log -S brainstorm-neo4j`: `45b9d92` (2026-03-06) removed the service | **Holds**, and has since 2026-03-06 |
| C3 | The local stack is one-click's compose plus its local override, which builds this server from a sibling `../brainstorm_server` | `CLAUDE.md:85–88`; `README.md:87–90` | One-click `origin/main:docker-compose.local.yml:3–4` (`context: ../brainstorm_server`). The same file also builds `../brainstorm_graperank_algorithm` (`:21–23`), `../Brainstorm-UI` (`:36–38`) and `../strfry` with `Dockerfile.neofry` (`:51–55`) | **Partly**: true, but three more siblings are needed (Non-blocking 2) |
| C4 | It runs Postgres, Redis, Neo4j, neofry, strfry, the GrapeRank worker and the UI | `CLAUDE.md:88–89` | `config --services` on an export of one-click `main`: those seven, plus `brainstorm-server`, `vespa`, `vespa-deploy` and `autoheal` | **Holds** (Vespa follows at `:98`) |
| C5 | The command: `docker compose -f docker-compose.yml -f docker-compose.local.yml up --build`, run in `../brainstorm_one_click_deployment` | `CLAUDE.md:91–94`; `README.md:94–97` | Both files are on one-click `main`, and the merged `config` parses (exit 0). It also parses with every build context missing, so a parse doesn't show the build would work | **Partly**, as C3. It's right for a checkout with all four siblings. Without the three the PR doesn't name, the build is expected to fail. That wasn't observed, since this review only parses (Non-blocking 2) |
| C6 | One-click can lag this server, and it leaves out some services that staging and production run | `CLAUDE.md:96–97`; `README.md:90–92`; en-nostr's comment | One-click has no search relay, dictionary relay or profile resolver, and this code relies on all three (`config.py:120`, `:173`; `backfill_redis_relationships.py:28`). The deployments themselves are unreadable (brainstorm-k8s 404) | **Holds as far as the repos show.** For the deployments, it rests on en-nostr's word |
| C7 | "When checked on 2026-11-12", one-click's `main` was last changed on 2026-07-21 | `CLAUDE.md:97–98`; PR body; thread | The local `origin/main` (fetched today) is `ec6f282`, committed 2026-07-21 15:36 +0200. GitHub agrees: the default branch is `main` at `ec6f282`, `committedDate` 2026-07-21T13:36:08Z | **Holds** |
| C8 | …and it still deployed Vespa | `CLAUDE.md:98` | `vespa` and `vespa-deploy` are in the base file (`:171–227`) and in the merged service list. `up` with no service names starts them | **Holds** |
| C9 | …which this server no longer uses | `CLAUDE.md:98–99` | No `vespa` anywhere in `app/`, `config.py` or `env.example`. Settings use `extra="ignore"` (`config.py:6–8`), so one-click's `vespa_url` is ignored | **Holds** |
| C10 | "its local override makes `brainstorm-server` wait on `vespa-deploy`" (also in `6d0b17d`'s message) | `CLAUDE.md:99–100` | The override writes `depends_on: !reset` and then a mapping of six services (`docker-compose.local.yml:13–19`). In the merged `config`, `brainstorm-server` has no `depends_on` key. The base file alone gives six dependencies, `vespa-deploy` among them. A two-file probe: under `!reset` the key is absent; `!override` gives the mapping | **Wrong** (Blocking 2) |
| C11 | To run on the host, stop the stack's `brainstorm-server`, which holds port 8000 | `CLAUDE.md:104–105` | One-click base `:5–6`: `8000:8000` | **Holds** |
| C12 | The stack's Neo4j bolt is on host port 7688 (README § How to run the tests) | `CLAUDE.md:106–107`; `README.md:74–77` | One-click **base** file on `main`, `:104`: `7688:7687`. So this is what one-click ships, not only this Mac's untracked dev-ports file. The README note names `NEO4J_DB_URL`, the real setting (`config.py:19`) | **Holds** |
| C13 | `cp env.example .env`, then `./start.sh`, which runs the migrations and then uvicorn | `CLAUDE.md:109–112` | `start.sh` runs `alembic upgrade head`, then `uvicorn … --port 8000`. env.example's Postgres, Redis and relay values match one-click's host ports (5432, 6379, 7777, 7778). Neo4j needs the 7688 edit | **Holds** |
| C14 | Production runs `latest`, which CI builds from `main` (`.github/workflows/build.yml`) | `CLAUDE.md:121–122` | `build.yml:98`: `latest` only when `github.ref` is `refs/heads/main`. Which tag production runs is in brainstorm-k8s, which I can't read | **Partly**: the CI half holds; the production half is unverified |
| C15 | Production takes a new `latest` only when its pod restarts, so a merge to `main` is not a production deploy | `CLAUDE.md:122–123` | No repo I can read shows it. brainstorm-k8s returns 404, and neither this repo nor one-click covers it. The PR itself marks it "observed from outside" | **Unverified** |
| C16 | `POST /recommend/pubkeys` has been on `main` since 2026-10-16, mounted unconditionally | `CLAUDE.md:124–125` | `d3fb8d1` (2026-10-16), merged in #105 (`c274d3c`, 2026-10-16). `open_ranking/router.py:38` and `routers/router.py:94–97` include it with no condition | **Holds** |
| C17 | Both `sessions/revoke` routes came with #106, on 2026-10-23 | `CLAUDE.md:125` | `8e8451f` (2026-10-23) adds `POST /user/sessions/revoke` and `POST /admin/users/{pubkey}/sessions/revoke` | **Holds** |
| C18 | On 2026-10-29, production still lacked those three routes, while staging served every route on `main` | `CLAUDE.md:123–126`; PR body | A past live state can't be replayed. Today (2026-11-12 23:40Z), both servers serve the same 99 operations on 88 paths, equal to `main`'s with Flash on, and production now has all three | **Unverified for its date.** Today's state is consistent with it |
| C19 | To see what each server runs, compare the live route lists at the two URLs with `main`'s `app.openapi()["paths"]` | `CLAUDE.md:126–130` | Both URLs answer 200 JSON, each with 99 operations on 88 paths. `main`'s list depends on settings. With `FLASH_ENABLED=false` (env.example and the config default) it's 83 operations on 74 paths. With `true` it's 99 on 88, and with `true` plus `DEPLOY_ENVIRONMENT=LOCAL` (env.example) it's 104 on 93. The recipe doesn't say whether to count operations or path keys (`app.openapi()["paths"]` gives path keys) | **Partly** (Non-blocking 4) |

### README.md

| # | Claim | Where | Evidence | Verdict |
| --- | --- | --- | --- | --- |
| C20 | The integration tests need the real local stack: Postgres, Neo4j and Redis | `README.md:46–47` | Three integration tests also talk to live relays: `test_relay_deletions_integration.py` (docstring `:3–4`, `init_nostr_client` at `:53`), `test_trusted_list_publish_integration.py` (`:3`, `:38–41`) and `test_trusted_list_content_publish_integration.py` (two relays, `:4–7`, `:52–53`). The DB tests need the schema at head (`test_shorturl_roundtrip.py:6`), and nothing in `tests/` migrates | **Partly** (Blocking 1) |
| C21 | "its stores are enough": `up -d postgres redis_strfry neo4j`, then `pytest -m integration` | `README.md:62–72` | The three service names exist on one-click `main`, and none needs a build. But they aren't enough (see C20). With a `.env` copied from env.example, the content-publish test makes that relay its lookup relay (`:52`, `:141`) and sends its three test events to `wss://wot.grapevine.network` (`:146–162`) | **Wrong** (Blocking 1) |
| C22 | The fast suite expects `PUBLIC_BASE_URL`'s host to be `localhost`, because the NWT-signing Open Ranking tests use that audience | `README.md:55–56` | `app/utils/auth/nwt.py:49–57` takes the audience from the hostname. `tests/conftest.py:122`: `TEST_AUD = "localhost"` | **Holds** |
| C23 | `tests/conftest.py` reads `.env` itself, and a value already in `.env` or the environment wins over its dummies | `README.md:56–58` | `conftest.py:37–45` loads `.env` with `setdefault`, then `:81–82` applies the dummies with `setdefault` | **Holds** |
| C24 | Any other host fails those tests (the body says "the 11 Open Ranking NWT tests") | `README.md:58`; PR body | `PUBLIC_BASE_URL=https://brainstormserver.nosfabrica.com`: 11 failed, 1781 passed. `http://127.0.0.1:8000`: the same 11. `http://localhost:9999`: all 1792 passed, so the host matters and the port doesn't. All 11 are in `tests/open_ranking/test_open_ranking_e2e.py` | **Holds** |
| C25 | Keep env.example's `http://localhost:8000` | `README.md:59` | `env.example:28`. But a `.env` equal to env.example fails 70 other fast tests (Non-blocking 3) | **Holds**, as far as it goes |
| C26 | "How to run everything with docker" is the one-click stack, checked out next to this repo | `README.md:85–97` | As C3 and C5 | **Partly**, as C3 and C5 |
| C40 | "Everything together (needs the stack up)": `poetry run pytest` | `README.md:79–83` (unchanged lines after the rewritten block) | `pyproject.toml`'s `[tool.pytest.ini_options]` has no `addopts`, so a bare run includes the 166 integration tests, with C20's and C41's needs | **Partly** (Blocking 1) |
| C41 | Implied by the integration steps: starting the stores and running the marker is enough to reach them | `README.md:62–77` | With no `.env`, conftest's dummies can't reach one-click's stores. Its `DB_URL` is `u:p@localhost:5432/test` (`conftest.py:48`), and its Neo4j is `bolt://localhost:7687` with password `neo4j` (`:56–58`). One-click uses `postgres`/`postgrespw` (base `:76–77`) and Neo4j on 7688 with password `password` (`:104`, `:106`). Redis's `localhost:6379` does match. The 7688 note at `:74–77` presumes a `.env`, but the integration steps never create one | **Wrong without a `.env`** (Blocking 1; it interacts with Non-blocking 3) |

### BIBLE.md and light-profile.md

| # | Claim | Where | Evidence | Verdict |
| --- | --- | --- | --- | --- |
| C27 | Tapestry's harness lives at `github.com/nousclawds4/tapestry`, "where tapestry has lived since 2026-10-23" | `BIBLE.md:89`; `light-profile.md:3`; PR body | Tapestry `1642cc1cb` and `c90bd889b` (2026-10-27) record "the move of 2026-10-23". `gh api repos/nousclawds4/tapestry` returns `full_name` **`TapestryProtocol/tapestry`**, created 2026-10-23T02:44:33Z, public, default `main`. Tapestry `e7dc1052b` (2026-11-11) records the move to the TapestryProtocol organization. It's on `github/staging`, the GitHub remote's `staging` as the local clone last fetched it, not on the clone's local `staging`. The new link 301s to `https://github.com/TapestryProtocol/tapestry/tree/main/engineering-team`. The old `nous-clawds4` address still answers 200, from a stale repo | **Partly.** True from 2026-10-23 to 2026-11-11; today the address only redirects (Non-blocking 1) |
| C28 | Story and decision files that cite the old address as history are left alone | PR body | `grep`: six lines in five story, decision and intake files remain, as stated (`stories/_intake.md` has two, at `:5` and `:15`). Two code docstrings also keep it (`app/services/duty_menu_build.py:6`, `app/services/self_map_build.py:5`), as a live pointer to where the spec lives, and so does `.claude/agents/gate-judge.md:3` (provenance) | **Holds as stated; incomplete** (Non-blocking 1) |

### engineering-team/OPEN.md

| # | Claim | Where | Evidence | Verdict |
| --- | --- | --- | --- | --- |
| C29 | `ErrorResponseSchema` is declared as the error model (`app/schemas/CLAUDE.md`, `app/services/CLAUDE.md` → Errors) but never used in live responses, and three error shapes exist | `OPEN.md:9` | `app/schemas/CLAUDE.md:38–42`; `app/services/CLAUDE.md:52–62`. The schema's only use is `responses=` at `app/routers/user/router.py:156–168`. The three shapes: a string `detail`, the 422 list, and ORE's `{"error": …}` with `X-Reason` (`open_ranking/errors.py`). The old pointer, `app/routers/CLAUDE.md`, never named the schema (`git log -S ErrorResponseSchema` on that file is empty), so the PR's change of pointer is a correct fix the description doesn't mention | **Holds** |
| C30 | The issue was filed on 2026-07-14 as #44, still open on 2026-10-29 | `OPEN.md:9`; PR body ("still open") | `gh issue view 44`: created 2026-07-14T03:00:13Z (UTC), `OPEN`, `closedAt` null, and the title matches the row | **Holds** (the date is UTC, the evening of 2026-07-13 in US Eastern time) |
| C31 | ORE-06 `/followers` and ORE-07 `/muters` take `results` from Neo4j but `total` from `scard followed_by:` / `muted_by:` | `OPEN.md:14` | `app/routers/open_ranking/followers_muters.py:56–57` and `:77–96` (`get_top_inbound_by_influence` and `_redis_inbound_count`, gathered together) | **Holds** |
| C32 | ORE `/stats/pubkey` and the user overview count from the same sets (`user_service.py`) | `OPEN.md:14` | `user_service.py:202–207` (`get_user_overview`) and `:277–281` (`get_user_rank_and_counts`, called at `open_ranking/stats.py:67`) | **Holds** |
| C33 | The one-shot startup backfill skips whenever prefix keys exist without its done marker | `OPEN.md:14` | `backfill_redis_relationships.py:150–168`, called once from the lifespan (`app/api.py:100`) | **Holds** |
| C34 | On 2026-07-14, production answered 5 results with `total` 0 for one pubkey, and on 2026-10-29 that pubkey agreed on both servers (14/14) | `OPEN.md:14` | This row is the only record left. The memory note it came from now points here, and records the test as run against `api.brainstorm.world`, which `BIBLE.md:21` calls production. The row doesn't name the pubkey, and ORE-06 is a POST, outside this review's read-only GETs | **Unverified** (Non-blocking 6) |

### The PR description and thread

| # | Claim | Where | Evidence | Verdict |
| --- | --- | --- | --- | --- |
| C35 | After the PR was retargeted from `main` to `staging`, `staging` at `a896204` was merged in (`74cacae`). There was one conflict, in OPEN.md, where both sides had added a row, and both rows are kept in date order. (`74cacae`'s message dates the retarget 2026-11-10) | PR body | GraphQL `BaseRefChangedEvent`: 2026-11-10T14:30:31Z, `main` → `staging`, by en-nostr. `74cacae`'s parents are `4973df4` and `a896204`. `git show --cc 74cacae` shows a resolution only in OPEN.md: the 2026-10-29 row, then the 2026-10-31 row | **Holds** |
| C36 | `6d0b17d` follows en-nostr's comment: CLAUDE.md and the README no longer call one-click's compose the full or whole stack | PR body; thread | `git show 6d0b17d`. A `grep` for "full" or "whole" near "stack" in both files finds nothing | **Holds** |
| C37 | The PR targets `staging`, so merging it builds only the `staging` and `sha-…` tags; CI tags `latest` only from `main` | PR body | `build.yml:5–7` (a push to any branch), `:93` (branch tag), `:97` (`sha-` tag), `:98` (`latest` on `main` only) | **Holds** |
| C38 | Docs only; no code changes | PR body | `git diff --name-only a896204 6d0b17d` lists five `.md` files. The code trees are identical by hash | **Holds** |
| C39 | This is the Doc lane, so a docs-mode Gate B review is owed | PR body | See the Gate A section below | **Holds** (ratified) |

## Evidence table

| Source or command | What it showed |
| --- | --- |
| `git log --graph a896204~3..6d0b17d`; `git merge-base a896204 6d0b17d` | Five commits on top of `a896204`. The first three sit on `6805f51` (`main`, #107), which `a896204` already contains |
| `git diff --name-only a896204 6d0b17d`; `git rev-parse {a896204,6d0b17d}:{app,tests,alembic,scripts,pyproject.toml,poetry.lock}` | Five Markdown files. Identical tree hashes for every code path |
| `gate.sh` on the worktree (log `pr120-review/gate-head-6d0b17d.log` in the session's scratch directory, not committed) | Five stages, each exit 0; 1792 passed, 166 deselected, 9 warnings |
| `docker compose -f docker-compose.yml config` (this repo, Compose 5.1.0) | Exit 1: `service "web" depends on undefined service "brainstorm-neo4j"` |
| `git log -S brainstorm-neo4j -- docker-compose.yml` | `45b9d92` (2026-03-06) removed the service. `web` still depends on it |
| One-click: `git show origin/main:{docker-compose.yml,docker-compose.local.yml,README.md}` | Ports: `8000:8000`, `5432:5432`, `6379:6379`, `7688:7687`, `7777`, `7778:7777`. Vespa services are in the base. The override has four build contexts and `depends_on: !reset`. The README's sibling list (`:41`) names `brainstorm_graperank` and has no `strfry` |
| `git archive origin/main` → scratch; `docker compose -f docker-compose.yml -f docker-compose.local.yml config` (and `--services`; and the base file alone) | Merged: exit 0, 11 services, and `brainstorm-server` has **no** `depends_on` key. Base alone: six dependencies, `vespa-deploy` among them. Both parse with every build context missing (the export has no siblings) |
| Two throwaway compose files in scratch, with `!reset` and with `!override` | With `!reset` followed by a mapping, the key is absent from the resolved config. `!override` gives the mapping. That's the tag's rule, not a one-click quirk |
| `gh api graphql` (one-click: visibility, default branch, open PRs) | Public. `main` at `ec6f282`, 2026-07-21T13:36:08Z. #12 ("name every sibling repo", 2026-10-29) is open |
| `git diff origin/main...nousclawds4/docs/sweep-david -- README.md` (one-click, local read) | #12 names four siblings: `brainstorm_server`, `brainstorm_graperank_algorithm`, `Brainstorm-UI` and `strfry` (the neofry fork) |
| Fast suite with `PUBLIC_BASE_URL` set to the production URL, `http://127.0.0.1:8000` and `http://localhost:9999` | 11, 11 and 0 failures. The same 11 node ids both times, all in `test_open_ranking_e2e.py` |
| Fast suite with env.example's 66 values in the environment (what a copied `.env` gives, by conftest's `setdefault`) | 70 failed: `test_billing_admin.py` 43, `test_flash_webhook.py` 23, `test_flash_client.py` 3, `test_billing_dev.py` 1. With the same values minus `FLASH_ENABLED`, `FLASH_API_KEY` and `FLASH_WEBHOOK_SECRET`: 1792 passed |
| `curl` GET of both `openapi.json` URLs, 2026-11-12T23:40Z | 200 JSON from both: 99 operations on 88 paths each, the same set |
| `dump_paths.py` (`app.openapi()["paths"]`) on an export of `origin/main` `1cf77fe` and on the PR head, under conftest's dummy settings | Counted as method-plus-path operations, with path keys in brackets: 83 (74) with Flash off, 99 (88) with Flash on, 104 (93) with Flash on plus `LOCAL`. `main` and the staging code have the same lists. Both live servers equal `main` with Flash on |
| `.github/workflows/build.yml`; `Dockerfile` | `latest` is gated on `refs/heads/main`. The `VERSION_ARG` and `GIT_SHA` build args aren't declared by the Dockerfile, so no version is exposed |
| `git log` on `recommend.py`; `git show 8e8451f` | ORE-04 on `main` 2026-10-16 (`d3fb8d1`, #105). The two revoke routes on 2026-10-23 (#106) |
| `app/routers/router.py`; `app/routers/open_ranking/router.py` | Open Ranking, ORE-04 included, is mounted with no condition. The billing routers mount only with `flash_enabled` (`router.py:50`), and the dev routes only under `LOCAL` (`:57`) |
| `gh issue view 44` | Created 2026-07-14T03:00:13Z, open, never closed. The title matches the row |
| Code reads: `followers_muters.py`, `user_service.py`, `stats.py`, `backfill_redis_relationships.py`, `api.py` | C31–C33 as stated |
| `git log -S ErrorResponseSchema` on each of the three `CLAUDE.md` files | `app/routers/CLAUDE.md` never named it. `app/schemas/` and `app/services/` did, and still do |
| Tapestry, local clone: `git log -S nousclawds4/tapestry` and `-S TapestryProtocol`; `gh api repos/nousclawds4/tapestry`; `curl -I` of the new and the old link | Moved to `nousclawds4` on 2026-10-23, then to `TapestryProtocol`, recorded 2026-11-11 by `e7dc1052b` (on `github/staging`, not the clone's local `staging`). The new link 301s to TapestryProtocol. The old link is 200, a stale repo |
| `grep` for tapestry addresses in the worktree | The list behind C28 |
| `gh api graphql` (PR #120's timeline; the repo's default branch) | Retargeted 2026-11-10 by en-nostr. The default branch is `staging` |
| `git show --cc 74cacae` | Only OPEN.md needed a resolution |
| `gh api repos/NosFabrica/brainstorm-k8s/git/trees/HEAD` | 404 for nousclawds4 |
| Integration test reads (`tests/integration/*`, `tests/conftest.py`) | Three tests need relays. The DB tests need migrations. HTTP tests use `ASGITransport`, so they need no live server |
| `env.example`; `app/core/config.py` | `FLASH_ENABLED=false` (`:61`) and empty Flash keys (`:64–65`). `NOSTR_TRANSFER_FROM_RELAY=wss://wot.grapevine.network` (`:19`). Every required setting is in one-click's compose |

## Gate A classification: ratified

The PR declares the Doc lane, and that's right. The diff is five Markdown files and changes no code, config, schema or
dependency. I checked the profile's triggers one by one:

| Trigger | Present? | Where |
| --- | --- | --- |
| A wire format or event shape | No | No event, TA or response is described differently |
| An auth or trust default | No | — |
| A schema or migration | No | — |
| A new dependency | No | `pyproject.toml` and `poetry.lock` are unchanged |
| A cross-repo contract | No | The text describes one-click's compose but changes no interface between the repos |
| Request routing or middleware ordering | No | — |
| Response headers or content-type | No | — |
| A value that exists in more than one repo | No | The docs quote one-click's ports, service and file names, but the change creates or alters none of them. Quoting another repo's values does mean the docs drift with it, which is why dating them matters (CLAUDE.md does that once, at `:97`) |

The light-profile edit changes only a link, not the rubric. The hotfix hatch doesn't apply. No trigger was missed.

## Repo-conventions integrity

*(This replaces tapestry's Concept-graph integrity, as the earlier reviews here did. This repo has no concept graph.)*

- [x] **The code conventions in `CLAUDE.md`** (async-only, Pydantic settings, lifespan-spawned tasks, `loggr`, the
  client tag) are untouched. No setting is added, so env.example needs no mirror.
- [x] **The "Add an env var" row** (`CLAUDE.md:80`, "the relevant compose file") still reads correctly. For the local
  stack, that file is now one-click's compose, which sets the server's environment inline. All 21 required settings
  are there today (`config.py:11–32` against one-click `docker-compose.yml:9–33`).

## Adversarial probes (beyond the diff)

**Premise errors.**

- *"The stores are enough."* The integration suite's premise is wrong, as Blocking 1 shows. The line the PR replaced
  listed strfry and neofry in its comment (`# postgres, redis, neo4j, strfry, neofry`). The new command drops them.
- *"The override makes the server wait on Vespa."* The PR read the override's text, not compose's resolution of it.
  The two differ (Blocking 2).
- *"Tapestry lives at nousclawds4/tapestry."* That was true when the PR was written. It went stale the day before the
  PR's 2026-11-12 refresh, which re-checked one-click but not this (Non-blocking 1, Harness friction 1).
- *"PUBLIC_BASE_URL is the `.env` hazard."* It is one. A `.env` copied from env.example breaks 70 more tests through
  the very mechanism the note describes (Non-blocking 3).
- *"Production pulls `latest` only on a pod restart."* No readable repo can confirm or refute it, and the PR says so
  itself. Today's live state (production serves every route on `main`) is consistent with it.

**Collateral damage: docs the change leaves wrong or contradicts.**

- **`README.md:131–150` ("Updating env vars")** still gives `brainstorm_server/docker-compose.yml` as "Local dev", with
  `docker-compose restart web`, `docker-compose build web` and `docker-compose up -d web`. That's the compose file the
  PR now says is refused as it stands. Compose loads and validates the project before any command, so each of these
  hits the same error (Non-blocking 5).
- **`README.md:5–17` ("How to run the project")** starts only a Postgres container, then the server on the host. The
  lifespan checks Neo4j at start (`app/api.py:96`) and the backfill needs Redis. Following that section alone, the
  server can't start. This predates the PR, but it's the README's first instruction, and it now disagrees with
  CLAUDE.md § Run locally (Non-blocking 5).
- **Six integration test docstrings** say "e.g. `docker compose up -d`" without saying which compose. That's
  harmless, now that the README names the stack. Not a finding.
- **Two code docstrings** still name `nous-clawds4/tapestry` as where the Duty Menu and self-Map specs live
  (Non-blocking 1).
- **Cross-repo:** one-click's own README names the wrong GrapeRank directory and leaves out `strfry`. One-click #12
  fixes that but is unmerged, so a reader sent there from this PR gets the wrong list (Non-blocking 2).
- **BIBLE §9** ("Run it: README (compose stack + `start.sh`)") still agrees.

**What a reader who follows the instructions would actually hit.**

1. **The full stack** (`CLAUDE.md:91–94`), with only this repo and one-click checked out. The build is expected to stop
   at the first missing context: `../brainstorm_graperank_algorithm`, `../Brainstorm-UI` or `../strfry`. That's
   standard compose behaviour, but I didn't observe it. This review only parsed, and `config` exits 0 even with every
   context missing. With all four siblings in place, the server starts without waiting for Postgres's health check,
   since the override leaves it no `depends_on`. Its `restart: unless-stopped` retries the migrations until Postgres
   answers. Neo4j asks for a 4 GB
   heap and an 8 GB page cache (base `:107–109`). The memory-reduction block in the override is commented out. That's
   the "tinkering" the PR warns of.
2. **The server on the host** (`CLAUDE.md:102–112`). The recipe works once `NEO4J_DB_URL` is moved to 7688.
   env.example's other values match one-click's host ports. env.example's `DB_URL` names no database, so the host
   server migrates and uses Postgres's default `postgres` database, not the stack's `brainstorm-database`. That's fine,
   but worth knowing.
3. **The fast suite in that same checkout**, which now has a `.env` from step 2. 70 billing and Flash tests fail, and
   the README's note points at a different cause (Non-blocking 3). With no `.env` the fast suite is green, but the
   integration run needs one (C41). Leaving the three Flash keys out of `.env` keeps the fast suite green either way.
4. **The integration suite** (`README.md:62–72`, and `:79–83`'s bare `poetry run pytest`, which runs the same tests).
   - With no `.env`, conftest's dummies can't reach one-click's Postgres or Neo4j (C41).
   - With one, three tests fail with no relay up.
   - If the `.env` came from env.example, the content-publish test first tries to send three test events to the public
     `wss://wot.grapevine.network`.
   - The DB tests fail on missing tables unless `alembic upgrade head` ran against the same `DB_URL` (Blocking 1).
5. **Comparing deployments** (`CLAUDE.md:126–130`). Built with env.example's settings, `main`'s list has 83 operations
   on 74 paths, against the live 99 on 88. So every live server seems to run 16 `/admin/billing` operations (14 paths)
   that aren't on `main` (Non-blocking 4).
6. **The tapestry links.** They work, by redirect (Non-blocking 1).

## Things tests can't catch

- [x] **No secrets.** The added text quotes ports, service names and paths only. One-click's dummy credentials aren't
  copied.
- [x] **No debug code or commented-out code** (not applicable to docs).
- [ ] **Error paths.** Following the integration and full-stack instructions fails in the ways the probes show
  (Blocking 1, Non-blocking 2).
- [x] **Concurrency** (not applicable).
- [ ] **Security.** The integration instructions, together with env.example, send test events to a public relay
  (Blocking 1). The README's "don't source a deployment's `.env`" is good advice, and it holds.

## House rules check

- [x] No new lint, typecheck or build tooling. No config or hook changes.
- [x] No ADR is owed (see the Gate A section).

## Template sections that don't apply

- **`npm test` / `gate:status`:** not applicable. This repo's gate is `poe check_all`, recorded above.
- **Playwright:** not applicable (no UI).
- **Concept-graph integrity and firmware reinstall:** not applicable (no concept graph). Replaced by Repo-conventions
  integrity above.
- **ADR adherence:** not applicable. The doc lane has no ADR, and no trigger fired.
- **Product-guide adherence:** not applicable (no PRD).
- **Test-deliverable carve-out:** not applicable (no test change).
- **Spec adherence:** replaced by the claims-adherence table, per the docs-mode variant.

## Findings

### Blocking

1. **`README.md:46–47`, `:62–72` and `:79–83`: the integration suite needs more than "its stores", and the instructions
   as written can send test events to a public relay.**
   - **What the README says.** The integration tests "require the real local stack: Postgres, Neo4j and Redis". Then:
     "its stores are enough", `up -d postgres redis_strfry neo4j`, `poetry run pytest -m integration`. Then
     "Everything together (needs the stack up)": `poetry run pytest`. With no `addopts` in `pyproject.toml`, that bare
     run includes the same integration tests.
   - **Settings.** The steps never create a `.env`, but the run needs one, or the same values in the environment.
     conftest's dummies can't reach one-click's stores. Its `DB_URL` is `u:p@localhost:5432/test` (`conftest.py:48`),
     and its Neo4j is `bolt://localhost:7687` with password `neo4j` (`:56–58`). One-click uses `postgres`/`postgrespw`
     and Neo4j on 7688 with password `password`. The 7688 note at `:74–77` already presumes a `.env`.
   - **Relays.** Three of the 166 integration tests talk to live relays:
     - `test_relay_deletions_integration.py` uses the TA relay, `settings.nostr_upload_ta_events_relay`, through
       `init_nostr_client`. With no local strfry, that client's connect assertion fails. The assistant-dictionary
       review's integration runs saw this.
     - `test_trusted_list_publish_integration.py` uses the TA relay.
     - `test_trusted_list_content_publish_integration.py` uses two relays: `settings.nostr_transfer_from_relay` as the
       lookup relay, and `trusted_list_relay` or the TA relay as the list relay.
   - **Migrations.** The DB-backed tests expect the schema at head (`test_shorturl_roundtrip.py:6` says so), and
     nothing in `tests/` creates it. The README's integration steps run no migration. The stack's server would run
     one, but these steps don't start it.
   - **The hazard.** CLAUDE.md:110 and README.md:13 have the reader copy env.example to `.env`. That makes
     `NOSTR_TRANSFER_FROM_RELAY` `wss://wot.grapevine.network` (`env.example:19`). The content-publish test then makes
     it the lookup relay (`:52`, `:141`), and sends its three test events there (`:146–162`): a note, a DList item
     and its header, signed by throwaway keys.
   - **Asked change.**
     - Say the run needs a `.env`, or the same values in the environment, pointing at the stack.
     - Say which tests need relays, and either have the reader start local ones or deselect those tests. One-click's
       `strfry` is on host port 7778, which is env.example's `NOSTR_UPLOAD_TA_EVENTS_RELAY`, and needs no build.
     - Tell the reader to point `NOSTR_TRANSFER_FROM_RELAY` at a local relay before running the suite.
     - Add `poetry run alembic upgrade head`, against the `DB_URL` the tests will use, before `pytest -m integration`.
     - Make `:46–47` and `:79–83` match.
2. **`CLAUDE.md:99–100`: "its local override makes `brainstorm-server` wait on `vespa-deploy`" isn't what compose does.**
   - **What the override says.** It writes `depends_on: !reset`, then a mapping of six services that includes
     `vespa-deploy` (`docker-compose.local.yml:13–19` on one-click `main`). `!reset` removes the attribute, and compose
     discards whatever follows the tag. `!override` is the tag that replaces.
   - **What compose does.** On an export of one-click `main`, `docker compose … config` (Compose 5.1.0) leaves
     `brainstorm-server` with no `depends_on` key at all. The base file alone gives it six, `vespa-deploy` among them.
     A two-file probe shows the same rule.
   - **What that means.** Under the override, the server waits on nothing, not even Postgres's health check. Vespa
     still runs, because `up` starts every service, so "still deployed Vespa" holds.
   - **Where else.** The wrong clause is also in `6d0b17d`'s commit message. That can stay as history.
   - **Upstream (one-click, not this PR).** The override evidently meant to replace the list, not empty it. After the
     tag it writes all six dependencies out again, with Redis's condition raised to `service_healthy` (the base has
     `service_started`). A plain merge or `!override` would have done that; `!reset` throws it away. That's a one-click
     bug, worth an issue there or a note on one-click #12. This review filed nothing (GitHub reads only).
   - **Asked change.** Correct or drop the clause. For example: "its compose still runs Vespa (`vespa`, `vespa-deploy`),
     which this server no longer uses. The base file makes `brainstorm-server` wait on `vespa-deploy`; the local
     override's `depends_on: !reset` drops all of its dependencies, so under the override it waits on nothing."
     Re-derive the replacement from `docker compose config` rather than taking this suggestion as given.

### Non-blocking

1. **`BIBLE.md:89` and `engineering-team/workflows/light-profile.md:3` (and the PR description): point the links at
   `github.com/TapestryProtocol/tapestry`.**
   - **Where it lives now.** The repo moved again, to the TapestryProtocol organization (recorded 2026-11-11).
     `nousclawds4/tapestry` only redirects to it.
   - **Why the redirect is fragile.** GitHub keeps a moved repo's redirect only until a repository is created at the
     old name. nousclawds4 forks the team's repos to open PRs, as it did for this one. A fork of tapestry under that
     account would take the name `nousclawds4/tapestry`, and these links would then show the fork. That's the same
     stale-copy problem the PR fixes: the old `nous-clawds4` address still answers 200, from a stale repo.
   - **Suggested.** A one-word change in each file. Also update the description's "where tapestry has lived since
     2026-10-23".
   - **The docstrings.** The description's "left alone" sentence covers story and decision files. It should also name
     the two code docstrings that still give `nous-clawds4/tapestry` as where the spec lives:
     `app/services/duty_menu_build.py:6` and `app/services/self_map_build.py:5`. They're live pointers, not history.
     A docs-only PR can't touch them, so say so, or open an OPEN.md row.
2. **`CLAUDE.md:86–88` and `README.md:88–90`: name the four sibling checkouts the override builds.**
   - The override builds `../brainstorm_server`, `../brainstorm_graperank_algorithm`, `../Brainstorm-UI` and
     `../strfry` (NosFabrica's neofry fork, with `Dockerfile.neofry`), each under exactly that directory name.
   - The PR names only the first. One-click `main`'s README names `brainstorm_graperank` and leaves out `strfry`.
     One-click #12, open since 2026-10-29, fixes that list.
   - Without the other three, the given `up --build` is expected to stop at the first missing context. That's standard
     compose behaviour, but not observed here: this review only parsed, and `config` exits 0 even with every context
     missing.
3. **`README.md:55–60`: the note covers 11 tests, but a `.env` copied from env.example fails 70 more.**
   - env.example sets `FLASH_ENABLED=false` and leaves `FLASH_API_KEY` and `FLASH_WEBHOOK_SECRET` empty (`:61`,
     `:64–65`). Through the `setdefault` rule the note describes, these beat conftest's dummies (`conftest.py:71–73`),
     so the billing and Flash tests fail: `test_billing_admin.py` 43, `test_flash_webhook.py` 23,
     `test_flash_client.py` 3 and `test_billing_dev.py` 1.
   - CLAUDE.md:110 tells the reader to make exactly that `.env`. It's an unchanged line, but inside the section this
     PR rewrote.
   - **Suggested.** Next to the `PUBLIC_BASE_URL` advice, say to leave `FLASH_ENABLED`, `FLASH_API_KEY` and
     `FLASH_WEBHOOK_SECRET` out of `.env`.
     - That works for the host server, where billing then stays off, the config default (`config.py:66–69`).
     - It works for the fast suite: with env.example's other values, 1792 passed.
     - The integration run then gets conftest's Flash dummies. Its two billing test files call the services
       directly, not the Flash-gated routes. That wasn't run here.
     - "Run with no `.env`" won't do, because the integration run needs one (C41).
4. **`CLAUDE.md:121–131`: make the comparison recipe reproducible, and say what it can't show.**
   - **Settings.** `main`'s list depends on settings. Build it with `FLASH_ENABLED=true` and a non-`LOCAL`
     `DEPLOY_ENVIRONMENT`, as both servers evidently run. With env.example's values it has 83 operations on 74 paths,
     against the live 99 on 88. With Flash on under `LOCAL` it has 104 on 93.
   - **Units.** Say which the reader counts. `app.openapi()["paths"]` gives path keys, and one path can carry several
     methods.
   - **What it can't show.** A route list shows routes, not commits. A fix that adds no route is invisible. Both
     servers report `info.version` `0.1.0`, and the image exposes no commit, because `build.yml` passes `GIT_SHA` and
     `VERSION_ARG` but the Dockerfile never declares them. So "to see which routes each server has" is accurate, and
     "to see what each server runs" overclaims.
   - **Which branch.** PRs now land on `staging`, the repo's default branch (this PR was retargeted there on
     2026-11-10), and staging pins a branch tag. So staging is better compared with the branch it pins than with
     `main`. They happen to agree today.
   - **Placement.** The production paragraph sits under "## Deploying to staging". A reader looking for production
     won't find it there.
5. **`README.md:131–150` and `README.md:5–17`: two README sections now contradict the PR's own claims.**
   - "Updating env vars" gives this repo's compose as the local-dev flow (`restart web`, `build web`, `up -d web`).
     Every one of those hits the project error the PR documents.
   - "How to run the project" starts only Postgres, and the lifespan needs Neo4j and Redis.
   - Both predate the PR, but they're in the file it edits, on the topic it corrects. Point them at CLAUDE.md § Run
     locally, or at the one-click equivalents with the service `brainstorm-server`.
6. **`engineering-team/OPEN.md:14`: record enough to re-run the live evidence, or drop it.**
   - The row's 2026-07-14 and 2026-10-29 observations name no pubkey or request, and their only other record has
     been moved into this row. Nobody can re-check "14/14".
   - Name the request (endpoint, pubkey, algorithm), or keep only the code-level description, which holds.

### Harness friction

*(For the operator to file. Each becomes an `engineering-team/OPEN.md` row of type `meta`; this review edits nothing
else.)*

1. **A refresh round has no rule for re-verifying external claims.** This PR was written on 2026-10-29 and refreshed on
   2026-11-12. The refresh re-checked one-click, but not the tapestry address, which had moved the day before. The doc
   lane could ask a refresh after a gap to re-derive every dated or external claim, or to say which ones it
   re-checked.
2. **The reviewer's scratch directory is the authoring session's.** The author's working files (`pr120_local_stack.py`,
   `pr120-body.md`, `pr120-reply.md`, `pr120-after.json`) sit beside the gate script I was given. Independence then
   rests on the reviewer not opening them. A separate scratch directory per role would remove the temptation.
3. **The deployment repo is unreadable to the account reviews run as.** `NosFabrica/brainstorm-k8s` returns 404 for
   nousclawds4, so no Gate B reviewer running as that account can check a claim about how staging or production takes
   images. CLAUDE.md and the README also send readers there. Either grant read access, or route such claims to
   whoever runs brainstorm-k8s, as this PR's own hedge does.
4. **The template still needs hand adaptation for the doc lane.** The assistant-dictionary review's friction 4 records
   this as known since shortest-path, and its friction 3 records the close-out mismatch. The docs-mode variant
   replaces only Spec adherence. ADR adherence, Concept-graph, Product-guide and "On PASS" each still need a manual
   "not applicable". "On PASS" also assumes a story file, and the doc lane has none.

## Independent check before the commit (applied)

This is `5-review.md`'s optional check, recommended for docs-mode. A read-only checker that isn't the Reviewer
re-derived the findings and the gate account, and audited this review's quotes, counts and line attributions. It
found no finding wrong, but it found wrong values, overclaims and gaps. I re-checked each point against the sources
before changing anything, and nothing in this file rests on its word.

**Folded in.** All of these held when I re-checked them:

- **Line numbers.**
  - The new OPEN row is line 14, not 13, at both `4973df4` and `6d0b17d`.
  - The content-publish test's three sends are at `:146–162`, with its lookup assignment at `:141`. I had cut off the
    third send.
- **Counts.**
  - C28's leftover tapestry addresses are six lines in five files (`_intake.md` has two).
  - 83, 99 and 104 are method-plus-path operations. The path keys number 74, 88 and 93, and the live documents have
    99 operations on 88 paths.
- **Blocking 2's mechanics.** What follows `!reset` is a mapping, not a list. Under the tag, the key is absent from
  the resolved config, not `{}`. My earlier probes printed `{}` only because my own scripts defaulted a missing key.
- **Blocking 1's reach.** It now covers `README.md:79–83` (a bare `poetry run pytest` runs the same tests) and the
  missing `.env`: conftest's dummies can't reach one-click's stores. Both are new claims rows, C40 and C41.
- **Observed versus expected.** "`up --build` fails" without the siblings was never observed, since `config` exits 0
  with every context missing. It's now "expected to stop", and C5 and C26 are "partly", like C3.
- **Non-blocking 3's suggestion.** "Run with no `.env`" would break the integration run. It now suggests leaving the
  three Flash keys out of `.env`. CLAUDE.md:110 is called an unchanged line inside the rewritten section, not new
  text.
- **Wording.**
  - Tapestry `e7dc1052b` is on `github/staging`, not the clone's local `staging`.
  - Harness friction 4's history: "known since shortest-path" comes from the assistant-dictionary review's friction 4.
  - The verdict no longer says I ran the base.
  - The gate log is named by its place in the scratch directory.
- **Upstream.** The one-click `!reset` bug is now noted under Blocking 2. I rested it on the override re-listing all
  six dependencies with a changed Redis condition, not on its Redis health check, which the GrapeRank worker's
  dependency also uses.

**Qualified, not taken as given.**

- *That leaving the Flash keys out "works for the integration suite".* That part is inferred from the tests' imports,
  not run here.
- *That the checker re-ran the gate with the same result.* I didn't verify its run. The gate account above is my own
  run.

**Rejected:** none.

Folding these in took no further GitHub calls. The recommendation is unchanged, and Blocking 1 is now broader.

## Verdict

**CHANGES_REQUESTED** (the reviewer's recommendation)

There are two blocking issues, and each is one or two sentences to fix.

- The README's integration instructions are wrong about what the suite needs. Combined with env.example, they send
  test events to a public relay (Blocking 1).
- One sentence in CLAUDE.md misstates how one-click's override resolves (Blocking 2).

Everything else holds or is close:

- The gate is green: 1,792 passed, matching the given staging baseline. The code trees are hash-identical to the
  base's, which is why I didn't run the base myself.
- This repo's compose really is refused, and the stack's ports, the Vespa leftovers, the CI tagging rule, the route
  dates, the OPEN rows' code paths and the merge all check out.
- The `PUBLIC_BASE_URL` note is accurate, down to the 11 tests and host rather than port.
- The production paragraph is honestly hedged.
- The Doc-lane classification is ratified.

Of the non-blocking items, I'd take 1 (the tapestry address, two words) and 3 (the Flash `.env` sentence) before merge,
since the PR is being refreshed anyway.

**Re-review scope after the fix:** the corrected sentences at `README.md:46–83` and `CLAUDE.md:96–100`, each re-derived
as a fresh claim from the tests, from `tests/conftest.py` and from `docker compose config`; any non-blocking item
taken; and `git diff --name-only` showing that only Markdown changed. No code gate re-run is needed unless a
non-Markdown file changes.

**Operator's verdict (Gate B):** see "Operator verdict (2026-11-12)" at the end of this file.

## On PASS

Not applicable to this recommendation, and not the reviewer's step in any case.

- The doc lane has no story file (tapestry 0-intake §3), so there's no `**Status:**` line to flip.
- Under the Light profile's Gate B, the operator gives the verdict, and committing this review is left to them. This
  file is uncommitted.
- `5-review.md`'s optional independent check before the commit (recommended for docs-mode) was applied. I verified
  each of its points against the sources and folded in what held; see "Independent check before the commit". The
  commit itself is left to the operator.

## Operator verdict (2026-11-12)

**CHANGES_REQUESTED**, given in the session as "Changes requested". The authoring session recorded it here and
committed this file on the operator's word. The fix takes both blocking findings and Non-blocking 1 and 3, and goes
back to this reviewer for re-review. Before the commit, the authoring session shortened two local paths in this file,
the worktree's and the gate log's, and the checker note about the second, since this repo is public. Nothing else in
the review changed.
