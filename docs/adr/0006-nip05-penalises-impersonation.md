# NIP-05 only ever counts against a user: impersonation zeroes, an unbacked claim halves

A NIP-05 identifier (`name@domain`) is a claim the domain is supposed to back: its
`/.well-known/nostr.json` maps the name to the user's key. Clients that don't check it show the
claim anyway, which is how impersonators borrow a known name — set `jack@cash.app` on a fresh key
and wait for clients that never ask cash.app. GrapeRank now uses the domain's answer against the
user, never for them:

| What the domain answers (`nip05_status`) | Effect on the user's Influence |
|---|---|
| The name belongs to **another key**, on two checks in a row (`IMPERSONATION`) | × 0, whatever the web of trust says |
| The name is **not backed**: missing from the domain's `names`, or a 404 (`NOT_LISTED`) | × 0.5 |
| The name maps to this key (`VERIFIED`) | none |
| No NIP-05, not fetched yet, domain down or a 5xx (`PENDING`), first sighting (`SUSPECTED`), or no usable answer — a 403, a redirect, an unreadable body, a non-key in `names`, a refused URL (`UNKNOWN`) | none |

`NosFabrica/brainstorm_profile_resolver` checks every graph user's NIP-05 nightly and records the
status on their `NostrUser` node; the GrapeRank worker (`NosFabrica/brainstorm_graperank_algorithm`)
reads it with the follow walk and applies the factor.

## Status

accepted

## Considered Options

- **Reward a verified NIP-05.** Rejected: a verified identifier proves only that someone controls a
  domain, which anyone can buy. A contradicted one is strong evidence; a confirmed one is weak.
- **Zero every mismatch.** Rejected: "mismatch" lumped three cases. Only a name the domain gives to a
  *different* key is impersonation; a name it doesn't list is a lapsed paid identifier or a typo; an
  unreadable answer is a broken server. Zeroing all three would wipe honest users.
- **Penalise every failed check.** Rejected: a 403 is usually a firewall blocking crawlers and a
  redirect a misconfiguration (NIP-05 says clients ignore redirects); neither is the user's doing.
- **Cut the published rank only.** Rejected: GrapeRank propagates. An impersonator whose own rank is
  hidden would still pass full trust to the accounts it follows, and its sybils would keep the lift.
  The factor multiplies Influence inside the sweep, so what a penalised user passes on shrinks too.
- **Zero on the first contradicting answer.** Rejected: a zero overrides the whole web of trust, and
  NIP-05s are otherwise re-checked monthly, so one bad day at a domain would cost its users a month.
  The first sighting is `SUSPECTED`, re-checked no sooner than 12 hours later (in practice the next
  night), and only a second agreeing answer makes it `IMPERSONATION`.

## Consequences

- **A broken domain is doubted, not believed.** If one domain names someone else for at least 10 of a
  run's checks and at least half of them, the resolver records `UNKNOWN` for them — suspicions written
  earlier in the run included — and logs the domain: that is a domain fault, not a crowd of
  impersonators. The count spans the run because a full pass meets one domain's users a few at a time.
- **Pinned users are exempt.** The Observer and the keys the Observer designated are the Observer's
  own choice and are never penalised.
- **Look-alike domains are not caught.** Whoever controls `jackdorsey.co` can verify `jack@` there.
  This catches only a name the real domain contradicts.
- **A zeroed user drops below the publish cutoff**, so their kind-30382 is deleted on the next run:
  clients see no rank, as for an unknown user, not an explicit "impersonator" marker. Publishing a
  marker is a separate decision.
- **The penalty is the same for every Observer.** It comes from the domain, not the web of trust.
- **A user can lift it** by fixing or removing the claim: a changed NIP-05 is re-checked the next
  night, and a removed one clears the status.
- **Constants, not presets.** The factors (0 and 0.5) are worker constants; promote them to
  GrapeRank preset parameters if someone needs to tune them.
- **No NIP-05 verdict is ever positive** — nothing reads `VERIFIED` for scoring.
