# Verified counts are preset-relative, with no validity floor

The verified follower / muter / reporter counts — published in Trusted Assertions and shown on profile
pages — count raters whose **Influence** clears the Observer's **preset per-relationship cutoff**: raw
Influence, strict greater-than, and **not** clamped up to the `0.02` publish/validity floor. "Verified"
is therefore relative to the Observer's chosen preset strictness (PERMISSIVE / DEFAULT / RESTRICTIVE) and
is **orthogonal to Valid** (Influence ≥ `0.02`, i.e. publishable). A verified rater need not itself be a
Valid user.

This reverses an earlier "Verified ⊆ Valid" decision that would have floored every verified cutoff at
`0.02`.

## Status

accepted

## Considered Options

- **Verified ⊆ Valid — floor every verified cutoff at `0.02`** (`max(preset cutoff, 0.02)`) — rejected. It
  contradicts the design rule that verified-muters/reporters are computed *exactly* as verified-followers,
  which already use the raw preset cutoff with **no** floor today (so a floor would silently change
  existing follower behavior). It also overrides a user-chosen preset param instead of using it as given.
  A hard schema `ge=0.02` would additionally **throw on load** of any existing sub-floor custom preset row.
- **Coercing floor** — migrate builtin preset rows to `0.02`, add a clamp-on-load validator, and clamp at
  compute — rejected as unnecessary machinery once the floor itself was rejected.
- **No floor (chosen)** — verified is a preset-relative count; the Observer's chosen cutoff is the only
  bar. Uniform with the followers/reporters counts that already ship.

## Consequences

- Under PERMISSIVE (cutoffs `0.002`), sub-`0.02` raters can count as verified. This is intentional —
  verified reflects the Observer's lens, not publishability.
- The verified-muters count consumes the previously-**unused** `verifiedMutersInfluenceCutoff` (DEFAULT
  `0.01`), which has never run in production. If its counts prove noisy, tune the **seeded preset value**
  deliberately — do **not** reintroduce a hidden clamp.
- No migration, schema validator, or compute-time clamp is added; the change is purely "start using the
  raw preset cutoff for muters, as we already do for followers/reporters."
- **Verified and Valid stay distinct concepts** (see `CONTEXT.md`): Valid = published (≥ `0.02`); Verified
  = clears the Observer's preset cutoff. A profile can show verified raters that are not themselves Valid.
- The tier low/unverified "verified line" uses the follower cutoff and can rise **above** the fixed tier
  bands under RESTRICTIVE; subjects below the line fall through to `unverified` regardless of the fixed
  bands — accepted, and the same "line wins" mechanic already in place, re-sourced from the preset.
