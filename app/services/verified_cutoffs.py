"""Preset → verified-cutoff resolution for the read endpoints.

A verified follower / muter / reporter is a rater whose own Influence, in the
observer's web of trust, clears the observer's preset cutoff *for that
relationship* — raw Influence, strict `>`, and deliberately no clamp against
the 0.02 publish/validity floor. That is how GrapeRank computes the counts it
publishes in a Trusted Assertion, so the read endpoints resolve the same three
cutoffs rather than trusting a client-supplied flat threshold.
"""

from dataclasses import dataclass

from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession as AsyncDBSession

from app.core.loggr import loggr
from app.core.tier_thresholds import DEFAULT_VERIFIED_THRESHOLD
from app.repos.brainstorm_nsec import get_graperank_preset_by_pubkey_on_db
from app.schemas.graperank_schemas import GrapeRankPresetParams
from app.services.graperank_preset_service import (
    normalize_preset,
    resolve_preset_params,
)

logger = loggr.get_logger(__name__)

# Inbound sections are rated *by* the other account, so each uses its own
# relationship's cutoff. Outbound sections (the subject rating others) use the
# follower cutoff — the general trusted-account bar.
_KIND_TO_CUTOFF: dict[str, str] = {
    "followed_by": "follower",
    "following": "follower",
    "muted_by": "muter",
    "muting": "follower",
    "reported_by": "reporter",
    "reporting": "follower",
}


@dataclass(frozen=True)
class VerifiedCutoffs:
    follower: float
    muter: float
    reporter: float

    @property
    def verified_line(self) -> float:
        """The low/unverified tier boundary — the general trusted bar."""
        return self.follower

    def for_kind(self, kind: str) -> float:
        # KeyError on an unknown kind is deliberate: silently falling back to
        # the follower cutoff would be a wrong count, not a missing one.
        return getattr(self, _KIND_TO_CUTOFF[kind])

    def as_kind_map(self) -> dict[str, float]:
        """The per-section cutoffs, as the repo layer wants them."""
        return {kind: self.for_kind(kind) for kind in _KIND_TO_CUTOFF}


# Used only when the observer's preset can't be read (unseeded table, DB blip).
# A public read shouldn't 500 over it; DEFAULT's seeded values are the baseline.
FALLBACK_VERIFIED_CUTOFFS = VerifiedCutoffs(
    follower=DEFAULT_VERIFIED_THRESHOLD, muter=0.01, reporter=0.1
)


def build_cutoffs_from_params(params: GrapeRankPresetParams) -> VerifiedCutoffs:
    return VerifiedCutoffs(
        follower=params.verifiedFollowersInfluenceCutoff,
        muter=params.verifiedMutersInfluenceCutoff,
        reporter=params.verifiedReportersInfluenceCutoff,
    )


async def resolve_verified_cutoffs(
    db: AsyncDBSession, observer: str | None
) -> VerifiedCutoffs:
    """The observer's saved preset, as three per-relationship cutoffs."""
    try:
        stored = (
            await get_graperank_preset_by_pubkey_on_db(db, observer)
            if observer
            else None
        )
        _, params = await resolve_preset_params(db, normalize_preset(stored), observer)
        return build_cutoffs_from_params(params)
    except (RuntimeError, SQLAlchemyError):
        # Unseeded preset table or a DB blip — serve DEFAULT rather than 500 a
        # public read. Anything else is a bug and propagates.
        logger.exception(
            "Could not resolve verified cutoffs for observer %s; using fallback",
            observer,
        )
        return FALLBACK_VERIFIED_CUTOFFS
