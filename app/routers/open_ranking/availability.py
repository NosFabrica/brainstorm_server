"""Pov-availability gate for personalized Open Ranking algorithms.

Contract (ORE-01 §"Point of View", as amended by Open-Ranking/protocol PR #9,
adopted as written):

- If the provider cannot serve the requested algorithm for the supplied pov,
  it MUST respond `422` and SHOULD explain why in `X-Reason`. It MUST NOT
  substitute a different point of view (global or default) and present the
  results as personalized — all-zero ranks or empty result sets under a
  personalized label are exactly that lie.
- If the pov is provisioned and its results are merely not ready yet, the
  provider SHOULD respond `202` with `Retry-After` (the endpoint OREs' existing
  "not yet available" mechanism).

An Open Ranking request never provisions state: unknown povs are an
enumeration / DoS surface, so there is NO auto-provisioning here. (NIP-50's
cold-start auto-provisioning is a deliberately different, out-of-band
behavior and remains untouched.)

Availability is per data source, because the sources go live at different
moments of the GrapeRank pipeline:

- Neo4j-backed endpoints (/stats/pubkey, /rank/pubkeys, /followers, /muters)
  are ready once the pov's calc has completed at least once
  (`BrainstormNsec.last_time_calculated_graperank`).
- /search/pubkeys is ready once the score mirror has landed in Vespa
  (`BrainstormNsec.is_observer_search_available`).

The house observer (`default_observer_pubkey()`) IS the global perspective:
its scores are recomputed on the periodic schedule and are the ones every
global algorithm serves, so it is always available as an explicit pov — it
may not even have a `brainstorm_nsec` row, and must never be gated on one.
"""

from __future__ import annotations

from fastapi import HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession as AsyncDBSession

from app.core.config import settings
from app.repos.brainstorm_nsec import get_pov_availability_fields_on_db
from app.repos.brainstorm_request_repo import any_request_in_pipeline_for_pubkey_on_db
from app.routers.open_ranking.capabilities import CAPABILITY_DOC, algorithm_requires_pov
from app.utils.observer import default_observer_pubkey

# Advisory retry hint for the 202 branch. A manual GrapeRank run typically
# completes well within a couple of minutes; 60s keeps polling clients close
# behind without hammering the provider.
RETRY_AFTER_SECONDS = 60


class PovComputing(Exception):
    """Personalized results for this pov are on the way — rendered as HTTP 202
    + `Retry-After` by the ORE exception handlers (see errors.py)."""

    def __init__(self, reason: str, retry_after: int = RETRY_AFTER_SECONDS):
        super().__init__(reason)
        self.reason = reason
        self.retry_after = retry_after


def _unavailable(reason: str) -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail=reason
    )


async def ensure_pov_available(
    db: AsyncDBSession, endpoint: str, algo_id: str, observer: str
) -> None:
    """Gate a resolved `(algorithm, observer)` pair per the unavailable-pov
    contract. No-op for global algorithms and for the house observer. Raises
    HTTPException(422) when the pov cannot be served, PovComputing (-> 202)
    when it is provisioned and a run is still in the pipeline.

    Applies to the *effective* observer, so authenticated mode (where the
    signer is forced as observer) gets the same never-substitute guarantee on
    personalized algorithms.
    """
    if not algorithm_requires_pov(algo_id):
        return
    if observer == default_observer_pubkey():
        return

    exists, graperank_ready, search_ready = await get_pov_availability_fields_on_db(
        db, observer
    )

    default_algo_id = CAPABILITY_DOC[endpoint][0]["id"]
    fallback = (
        f"fall back to the endpoint's default algorithm '{default_algo_id}' "
        f"for a global view"
    )

    if not exists:
        raise _unavailable(
            f"algorithm '{algo_id}' cannot be served for pov {observer}: no "
            f"scores exist for this point of view and none are scheduled "
            f"(ranking requests never provision new povs). Either {fallback}, "
            f"or provision this pov at {settings.frontend_url}"
        )

    ready = search_ready if endpoint == "/search/pubkeys" else graperank_ready
    if ready:
        return

    if await any_request_in_pipeline_for_pubkey_on_db(db, observer):
        raise PovComputing(
            f"scores for pov {observer} are still being computed; retry the "
            f"identical request after {RETRY_AFTER_SECONDS} seconds"
        )

    if graperank_ready:
        # Only /search/pubkeys can land here: calc done, but the Vespa mirror
        # never went live and nothing is in flight to repair it.
        raise _unavailable(
            f"algorithm '{algo_id}' cannot be served for pov {observer}: "
            f"scores exist but are not yet available in the search index, and "
            f"no publication is in progress. Either {fallback}, or trigger a "
            f"new run at {settings.frontend_url}"
        )

    raise _unavailable(
        f"algorithm '{algo_id}' cannot be served for pov {observer}: scores "
        f"have not been computed for this point of view and no computation is "
        f"in progress. Either {fallback}, or trigger a run at "
        f"{settings.frontend_url}"
    )
