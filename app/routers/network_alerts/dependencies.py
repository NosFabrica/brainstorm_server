"""Observer + preset resolution for `/networkAlerts`.

The observer is a query param, not the JWT viewer, so this can't reuse
`user/dependencies.get_verified_cutoffs`. Split in two so tests can override the
DB half and still run pubkey validation for real.
"""

from fastapi import Depends, Query
from sqlalchemy.ext.asyncio import AsyncSession as AsyncDBSession

from app.core.database import get_db
from app.services.verified_cutoffs import (
    VerifiedCutoffs,
    resolve_verified_cutoffs,
)
from app.utils.nostr import resolve_pubkey_or_400


def resolve_alert_observer(
    observer: str = Query(
        ...,
        description=(
            "Observer pubkey (hex or npub). All trust scores are reported from "
            "this perspective. Pass the House pubkey for the House view."
        ),
    ),
) -> str:
    """The observer query param as a hex pubkey; 400 if it isn't one."""
    return resolve_pubkey_or_400(observer, "observer")


async def get_alert_cutoffs(
    observer: str = Depends(resolve_alert_observer),
    db: AsyncDBSession = Depends(get_db),
) -> VerifiedCutoffs:
    """The observer's saved-preset cutoffs — never a client-supplied threshold."""
    return await resolve_verified_cutoffs(db, observer)
