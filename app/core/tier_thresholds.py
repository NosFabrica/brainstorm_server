"""Canonical tier-band thresholds.

Bucket names match the GR result writer's `count_values` keys
(see app.message_queue_tasks.message_queue_consumer). Each constant below
is the LOWER bound of its tier — `influence >= TIER_HIGH` means tier "high",
`influence >= TIER_MEDIUM_HIGH AND < TIER_HIGH` means tier "medium_high",
etc.

DEFAULT_VERIFIED_THRESHOLD is the lower bound of "medium_low" but is kept
named separately because it's the verified line. `/user/{pubkey}/stats` no
longer uses it: that endpoint derives the line (and the three per-relationship
verified cutoffs) from the observer's saved GrapeRank preset — see
app.services.verified_cutoffs. It happens to equal DEFAULT's seeded follower
cutoff, so the DEFAULT baseline is unchanged. `/overview` and `/connections`
still take it per request until ticket 03 migrates them.

Single source of truth for both:
  - on-the-fly tier counts in /user/{pubkey}/stats and /user/{pubkey}/connections
  - per-hop confidence buckets written into BrainstormRequest.count_values
"""

TIER_HIGH = 0.50
TIER_MEDIUM_HIGH = 0.20
TIER_MEDIUM = 0.07
DEFAULT_VERIFIED_THRESHOLD = 0.02
