# The Light profile (trial)

> **Status:** trial — this repo is the trial environment for the Light strictness profile proposed by the 2026-08-18 harness review ([Harness Review & the Light Profile](https://claude.ai/code/artifact/f650d6c4-80dd-494e-a23a-7a30a0848710)). Roles, phase definitions, and templates continue to run **by reference** from [tapestry's `engineering-team/`](https://github.com/nous-clawds4/tapestry/tree/main/engineering-team) (the pattern the shortest-path book proved); this file defines only what Light changes. If Light graduates, this file and the vendored gate-judge move to the shared teams home (planned as a Claude Code plugin) and tapestry's `0-intake.md` §3 gains the profile. If it doesn't, delete both and revert to Standard.

**The design principle:** count *human stops* and *rejection points* separately. The availability tax is paid in human stops; the defect catches live at rejection points. Light keeps a rejector at every gate position that demonstrably rejects in the tapestry corpus, but staffs the interior ones with the blinded gate-judge instead of a waiting human.

## Lanes

| Type | Path | Human stops |
|---|---|---|
| Feature | **Gate A** (human) → J1 design → J2 test plan → J3 post-implementation → **Gate B** (human review) | 2 |
| Bug | Implementer + Reviewer | 1 (Gate B) |
| Refactor | Implementer + Reviewer | 1 (Gate B) |
| Doc / one-liner | Implementer + Reviewer, docs-mode review variant | 1 (Gate B) |

The hotfix hatch is unchanged from the ladder: operator present, trivial change, one OPEN.md row or intake line naming the commit.

## Gate A — scope and approach (human)

Before any interior work. The operator approves, in one exchange: the story's scope (one subsystem, bounded), the intended approach in a sentence or two, and the **classification** — Design note or ADR, per the irreversibility triggers below. This is the corpus's most productive rejection position (6 of 14 kick-backs); rejecting here costs minutes, not phases.

**Irreversibility triggers — any one requires a full ADR (with Options considered) and escalates the story to Standard phases:** a wire format or event shape; an auth/trust default; a schema or migration; a new dependency; a cross-repo contract; request routing or middleware ordering; response headers or content-type; any value that exists in more than one repo. Everything else: a **Design note** — 3–6 bullets in the story file: chosen approach, one rejected alternative, blast radius. The classification is provisional here and **ratified by the Reviewer at Gate B**; a wrong call costs one line ("ADR owed — escalate"), not a shipped default. Tripping a trigger mid-story escalates immediately.

## The judged interior (no human stop)

Each interior gate is one fresh spawn of the [gate-judge](../../.claude/agents/gate-judge.md) against the matching rubric below. APPROVE proceeds; KICK_BACK loops the phase with the verdict's findings; a judge HALT (or two consecutive KICK_BACKs on the same phase) pages the human. Spawn prompts carry only: the gate name, this file's rubric section, the artifact paths, and the story file — never progress state, deadlines, or the other gates' outcomes.

### J1 — design (after the Design note / ADR)

1. The note/ADR states an approach concrete enough to test against — named modules/endpoints, not intentions.
2. One alternative is named and the rejection reason is real (not a strawman).
3. Blast radius names every consumer the diff will touch; grep-verify at least one claimed non-consumer.
4. No irreversibility trigger is present that the classification missed (check the list above against the approach).
5. For an ADR: Options considered and Consequences sections present per tapestry's template.

### J2 — test plan (after the plan)

1. The edge-case / regression-sentinel / not-covered section exists and contains at least one scenario **not** derivable from any acceptance criterion.
2. Error paths for every external dependency the design touches (DB down, queue empty, relay unreachable, malformed input) are either covered or explicitly listed as not-covered with a reason.
3. Every AC has at least one handle in the AC→handle lines (`AC-3 → U1, U2`).
4. The planned assertions would **fail against the current (pre-implementation) code** — spot-check one: a plan that passes before the work exists is measuring nothing.
5. For a test-deliverable story: the guard-suite carve-out applies (tapestry `templates/adr.md`) — the plan names the guard suite and bars Phase 4 from it.

### J3 — post-implementation readiness (before review)

1. The full gate command (`poe check_all`) run in the foreground, exit code captured by brace-redirect — **never piped through `tail`** — and green.
2. The diff stays inside the blast radius the design note declared; anything outside it is named and justified in the story file.
3. New/changed tests correspond to the plan's handles; no planned handle silently dropped.
4. No skipped/vacuous test masks a red result (grep the run output for skip markers touching changed areas).

## Gate B — review (human verdict)

The review runs at **full rigor, always** — independence is the point, and its floor is non-negotiable at every tier: gates re-run by the reviewer with recorded results; the AC verdict table (or the **claims-adherence table** for docs, per tapestry's review-template docs-mode variant); the evidence table. The reviewer also **ratifies the Gate-A classification** — confirming no irreversibility trigger was missed — and probes adversarially beyond the plan (the corpus's unique review catches are premise errors, collateral damage outside the diff, and error-path probing; emulate them). For small mechanical changes the *narrative* may be capped (~300 words); the tier is assigned after the review is written, so misclassification costs nothing. Docs/spec and security-adjacent stories always get full-depth prose.

## Artifacts

One story file per story — story + Design note + edge-case list + AC→handle lines — plus one review file. ADRs in `decisions/` only when a trigger fires. Books (`audits/<slug>/book.md`) only for multi-story efforts. Intake (`stories/_intake.md`) unchanged.

## Instrument preconditions

A story does not open on a red baseline: run `poe check_all` first; a red baseline is its own bug/refactor story. Gate commands must finish inside tool timeouts and report true exit codes (brace-redirect capture; never `| tail`). This repo's pytest/`poe check_all` meets these today — keeping it that way is part of the trial.

## Trial protocol

1. **First: one control book under Standard** (all phases, by reference from tapestry), so Light is compared against Standard *in this repo* rather than against tapestry's differently-instrumented history.
2. Then **2–3 books under Light** from the backlog.
3. **Evidence metrics:** findings-per-review at Gate B (tapestry's corpus median is 3; consistently empty Light reviews mean the gates lost their teeth) and escaped defects (OPEN.md rows attributed to a Light story within 30 days of its PASS). Wall-clock and artifact mass are recorded as *descriptive* outcomes only — they are the treatment, not the evidence. Do not read tapestry `harness-stats.sh`'s headline "kick-back rate" as the comparison line: it reports CR-final ÷ decided (~1% on the corpus); the comparable figure is its kick-back *history* line (~17%).
4. Each Light book's audit carries a §7 process-findings section, as usual — the trial's own frictions get recorded the same way tapestry's were, because that ledger is what made this profile designable.
