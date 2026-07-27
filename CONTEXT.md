# Server

Backend API that ingests Nostr events, maintains the trust graph, drives GrapeRank computations, and
publishes the resulting trust scores back to Nostr as signed events.

## Language

**Trusted Assertion (TA)**:
A signed Nostr kind-30382 event in which an Observer asserts a trust score for a single Observee. The
event's `d` tag is the Observee, and the `rank` tag is the score. One TA per Observer→Observee pair.
_Avoid_: assertion event, trust event, score event

**Observer**:
The user from whose perspective trust is computed and asserted. TAs are signed by the Observer's
dedicated assistant key, one per Observer.
_Avoid_: source, truster, viewer

**Observee**:
The user a Trusted Assertion is about — the subject being scored.
_Avoid_: target, subject, scoree

**Publish run**:
The end-to-end processing of one Observer's GrapeRank result into Trusted Assertions: signing the
above-cutoff scores as TAs, deleting the ones that fell away, and persisting that the run published.
This is only the publish stage of a Brainstorm request, not the whole lifecycle.
_Avoid_: upload, batch, job

**Brainstorm request**:
One invocation of the full GrapeRank lifecycle for a single Observer — calculate scores, then the
Publish run. It is the unit the Scheduler admits and a worker processes. Moves through the request
states below. A request has a trigger source (periodic / scheduled / manual / admin).
_Avoid_: job, task

**Request states**:
The lifecycle a Brainstorm request moves through. Calculation is tracked by the request's status;
publishing by a parallel TA-publication status.
- **Waiting** — created and enqueued, not yet picked up (queued behind the admission limit, or awaiting a worker).
- **Ongoing** — a worker has picked it up and is calculating / publishing.
- **Success** / **Failure** — terminal. A Failure is terminal even if TA-publication never left Waiting.

**In-flight** (a.k.a. in-pipeline):
A request still progressing — Waiting or Ongoing, or Success with TA-publication still Waiting/Ongoing.
The Scheduler counts in-flight *scheduled* requests as backpressure and pauses admission while any exist.
_Avoid_: pending, active

**Abandoned request**:
A non-terminal (In-flight) request whose worker has died, so it will never progress on its own — e.g. a
worker that popped the job off its Redis lane, then was killed mid-run (a deploy or crash). Distinct from
a merely **queued** request, which is legitimately Waiting with its payload still in the Redis lane. The
tell: a queued request's payload is still in the lane; an abandoned one's has been popped and lost.
_Avoid_: stuck (ambiguous — a queued request also looks "stuck"), hung, orphaned ("orphan" already
means dead Vespa/relay TA cells in this context — a data-sink concept, not a request)

**Reap**:
To force an Abandoned request terminal (Failure, with an admin reason) so it stops counting as In-flight
and unblocks Scheduler admission. Reaping does **not** stop a live worker — it only marks the row; a
terminal (reaped) row cannot be resurrected by a late worker write-back.
_Avoid_: cancel (over-promises that in-progress work stops), abort, kill

**Scheduler**:
The leader-locked loop that admits overdue Observers as scheduled Brainstorm requests, up to an in-flight
target (backpressure). Yields to interactive (manual/admin) runs. Periodic runs bypass its admission gate.

**Influence**:
The continuous trust score in [0,1] the GrapeRank calculation assigns each user from a given Observer's
perspective. Internal to the algorithm, and the scale on which preset cutoffs are expressed.
_Avoid_: score (reserve for Rank), weight, trust

**Rank**:
The published integer 0–100, `round(Influence × 100)` — the quantum that appears in a Trusted Assertion's
`rank` tag and in the Vespa cell. The network's resolution is 1 rank point = 0.01 Influence.
_Avoid_: score, influence (Rank is the rounded, published form of Influence)

**Valid user**:
A user whose Influence clears the `0.02` publish cutoff (`CUTOFF_OF_VALID_GRAPERANK_SCORES`). Below it a
user is not published as a Trusted Assertion. Distinct from Verified (below).
_Avoid_: verified (a separate, preset-relative concept), trusted, published-worthy

**Verified follower / muter / reporter**:
For a given Observer and subject, the count of accounts following / muting / reporting the subject whose
own Influence, in that Observer's web of trust, clears the Observer's preset cutoff for that relationship
(raw Influence, strict `>`). "Verified" is relative to the Observer's chosen preset strictness and is
**orthogonal to Valid** — a verified rater need not itself be a Valid user. See ADR 0001.
_Avoid_: trusted follower (ambiguous with the tier bands), confirmed, real

**Hops**:
The follow-path distance from the Observer to the subject in the web of trust: 0 = the Observer, 1–8 =
reachable depth, and a sentinel `≥ 999` = unreachable within the hop limit. Published in a Trusted
Assertion's `hops` tag (omitted when the sentinel).
_Avoid_: distance (unqualified), degree, depth
