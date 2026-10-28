# Story 1: Your Assistant's Dictionary — the Assistant signs concept copies

**Status:** Draft (Gate A approved 2026-10-27)
**Created:** 2026-10-27
**Type:** Feature
**Source spec:** [NosFabrica/brainstorm_server#112](https://github.com/NosFabrica/brainstorm_server/issues/112),
from Brainstorm-UI's `docs/dictionary/ASSISTANT-COPY-ASK.md`
([NosFabrica/Brainstorm-UI#299](https://github.com/NosFabrica/Brainstorm-UI/pull/299))
**Base:** branched from #110 (`feat/settings-updates`) for the Assistant's relay I/O; rebased onto `main` once #110
merges.
**Classification:** ADR (irreversibility triggers: a wire format or event shape, a cross-repo contract, request
routing, an auth/trust default (what the Assistant signs, at whose request), and a value that exists in more than one
repo (the copy rule's tag list)). The story runs the **Standard** phases per
[`workflows/light-profile.md`](../../workflows/light-profile.md) §"Gate A". ADR:
[`decisions/assistant-dictionary/0001-the-assistant-signs-concept-copies.md`](../../decisions/assistant-dictionary/0001-the-assistant-signs-concept-copies.md).

## Background

Every customer has a Brainstorm Assistant: the Observer's dedicated assistant key (`brainstorm_nsec`), the one its
Trusted Lists and Duty Menu are signed with. Since #110:

- `GET /setup/{pubkey}` gives the Assistant the customer's Concepts duty (`39998` and `39999` at
  `settings.dictionary_relay`);
- the Assistant's Duty Menu offers that duty.

But the Assistant publishes no concept header yet, so its Treasure Map lists Concepts as `not-published-here`.

A concept is in someone's **Dictionary** when they hold a **local copy** of it: a kind-39998 header at the community
header's `d`, pointing back with `["b", <community coordinate>, "pointer"]`. Brainstorm-UI already reads Dictionaries
this way: Settings › Dictionary, item pages and search read the customer's own copy first, then their Assistant's.
Its "Add to my Dictionary" (Brainstorm-UI#299) has a line for each Dictionary:

- the customer's own, which they sign in the browser;
- the Assistant's, the preferred one, which only this server can sign.

Until the routes below exist, the Assistant's line says it can't add concepts on this server yet.

The product decision is Brainstorm-UI ADR 0009, accepted 2026-10-27 for Loom's K2.6:

- concepts are added by hand;
- the Assistant is the caller's own;
- the copy is exact, with a `pointer` `b` (Tapestry's header contract, `protocols/drafts/assistant-designation.md` §
  "Per-DList curation entries");
- adding is affiliation, not a vote;
- removing is withdrawing (`["b", "b-tag-deferred"]`), never a NIP-09 deletion.

## User-facing description

As a Brainstorm customer, I want my Brainstorm Assistant to add a community concept to the Dictionary it keeps for
me, and to remove it again, so that the concepts I use are defined by my Assistant's copy without my signing
anything.

## Vocabulary

Per [`CONTEXT.md`](../../../CONTEXT.md), Brainstorm-UI's `CONTEXT.md` ("Concepts and the Dictionary") and Tapestry's
`protocols/drafts/inherit-from.md`:

- **Community concept** — the kind-39998 header other headers point at, named by its coordinate
  `39998:<pubkey>:<d>`. A `d` may contain colons.
- **Local copy** — someone's own kind-39998 header at the community header's `d`, carrying
  `["b", <community coordinate>, "pointer"]`.
- **The Assistant's Dictionary** — the community concepts the Assistant holds a local copy of.
- **Withdrawn copy** — the same coordinate republished with `["b", "b-tag-deferred"]` in place of the pointer:
  "considered, affiliated with none".

## Acceptance criteria

Testable from the outside. Each criterion gets at least one test handle.

- [ ] **AC1 — adding publishes an exact copy.** `POST /user/assistantDictionary` with a community coordinate signs,
      as the caller's Assistant, a kind-39998 event at the community header's `d`. Its tags are, in this order:
      - the community header's `names`, `name`, `description`, `slug`, `required`, `recommended`, `optional`,
        `allowed`, `field-type`, `display`, `image` and `link` tags, verbatim and in the header's order;
      - `["b", <community coordinate>, "pointer"]`;
      - the client tag;
      - nothing else.

      The answer is `published`, with the signed event and a per-relay report.
- [ ] **AC2 — adding is idempotent.** A second add with nothing changed answers `unchanged` and publishes nothing.
- [ ] **AC3 — the copy follows the community when asked.** After the community header changes, an add answers
      `updated` and republishes the same coordinate.
- [ ] **AC4 — never re-points.** When the Assistant's header at that `d` points elsewhere or carries no `b`, nothing
      is published. The answer is `conflict`, with a sentence.
- [ ] **AC5 — removing withdraws.** `POST /user/assistantDictionary/withdraw`:
      - when the Assistant's copy points at the concept, republishes the coordinate with `["b", "b-tag-deferred"]`
        and the withdrawn `alt`, and answers `withdrawn`;
      - with no copy, or one already withdrawn, answers `unchanged`;
      - when the header there points elsewhere, answers `conflict`.
- [ ] **AC6 — adding after a withdrawal** publishes a fresh copy (`published`).
- [ ] **AC7 — an unknown concept.** A coordinate whose community header can't be found answers `not_found` and
      publishes nothing.
- [ ] **AC8 — input.** A body naming anything other than kind-39998 coordinates answers 400. A `d` containing colons
      is kept whole.
- [ ] **AC9 — routing.** `GET /user/{pubkey}` answers neither path, and both routes refuse a request without a
      session.
- [ ] **AC10 — where it goes.** The event is published to `settings.dictionary_relay` and the Assistant's outbox
      relays, dated `max(now, newest version + 1)`. The answer is 502 when no relay took it.
- [ ] **AC11 — it matches the UI.** For the fixture header in Brainstorm-UI's `lib/conceptCopy.test.ts`
      (`exactCopyTemplate`), the server's copy has the same tags that test expects.

## Approach

Two routes on the authenticated `/user` router, beside the Duty Menu's, built from two modules in the
`duty_menu_build` / `duty_menu_service` split:

- **`dictionary_copy_build`** is pure, with no I/O. It owns the coordinate grammar, the copy's tags and the withdrawn
  tags, and the outcome given what the Assistant already holds.
- **`assistant_dictionary_service`**:
  - reads the community header and the Assistant's newest version from the dictionary relay;
  - signs with the Assistant's key;
  - publishes through the Duty Menu's relay I/O (`connect_targets`, `send_event`, `relay_report`);
  - handles one press at a time per Assistant.

## Gate-command deviation (approved)

On the base (#110's head, `e2ca872`), `poe check_all` is red only where it needs services:

- `black`, `isort`, `flake8` and `mypy` pass (`mypy`: no issues in 192 source files).
- `pytest ./tests`: 1,494 passed, 47 failed and 96 errors.

All 143 failures and errors are under `tests/integration/`, which needs a live Neo4j (`127.0.0.1:7687`), Postgres,
Redis and strfry. None of those runs on the machine building this story: Docker's CLI is installed, but no daemon is
running. `pytest -m "not integration"` deselects exactly those 143 tests.

**Operator decision (Gate A, 2026-10-27):**

- The story's gate is `poe check_all` with the integration tests deselected.
- Parity evidence stands in for the rest: the same 143 integration tests fail before and after this diff.
- This story touches no Neo4j, Postgres or Redis path.

This follows the precedent of the Trusted Lists story (Vinney, 2026-08-26). Bringing up the local stack
(`brainstorm_one_click_deployment`) for a fully green run was the alternative, and it was not chosen.

## Out of scope

- The admin route for the same copies (`POST /admin/dictionary/{observer}`, Brainstorm-UI's
  `docs/dictionary/ADMIN-ASKS.md`).
- Items (curation copies), editing the Assistant's copy, and keeping a copy in step when the community header changes.
  Adding again answers `updated`.
- Treasure Map entries (`["39998:<d>", <Assistant>, <relay>]`) and the self-Map's Concepts line.
- A `GET`: the UI reads the Assistant's copies from relays.

## Open questions (to resolve during the ADR)

1. **Strict or lenient reads.** If the dictionary relay doesn't answer, should the route refuse (502) rather than
   publish over a header it couldn't see? The self-Map reads strictly and the Duty Menu leniently. AC4 argues for
   strict.
2. **The answer's `relays`.** The ask lists URLs, while the Duty Menu answers `{url, ok, message}` per relay. Should
   it align on the Duty Menu's shape, with the UI's type in #299 updated to match?
3. **Publish targets.** Beyond the dictionary relay and the outbox, should it also publish to every relay an earlier
   version was found on, as the Duty Menu does?
4. **Several concepts in one call.** One event per concept, with a status per concept. Is the 502 only for when
   nothing was published?
5. **`alt` on an added copy.** The withdrawn copy carries one. Should an added copy describe itself too, or carry
   none, as the UI's own copy does today?
