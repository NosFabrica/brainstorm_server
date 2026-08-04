"""Network Alerts panel (Brainstorm dashboard).

Mounted at root so the path is exactly /networkAlerts — like /shortestPath,
this is a graph-wide question parameterized by an observer, not a lookup on the
/user/{pubkey} resource.

The observer is an explicit query param rather than the authenticated caller:
the dashboard renders the House point of view by passing the House pubkey, and
the endpoint has no reason to know which pubkey that is. Public read, matching
the auth posture of the other read-only graph traversals — every score it
returns is already public via /user/{pubkey}/overview.
"""

from fastapi import APIRouter, Depends, Query

from app.routers.network_alerts.dependencies import (
    get_alert_cutoffs,
    resolve_alert_observer,
)
from app.schemas.request_response_schemas import GetNetworkAlertsResponse
from app.services import network_alerts_service
from app.services.verified_cutoffs import VerifiedCutoffs
from app.services.network_alerts_service import (
    DEFAULT_ALERT_LIMIT,
    MAX_ALERT_LIMIT,
)

router = APIRouter()


@router.get(
    path="/networkAlerts",
    summary=(
        "Pubkeys in the observer's network carrying more verified reports "
        "than their reach justifies"
    ),
)
async def get_network_alerts_endpoint(
    observer: str = Depends(resolve_alert_observer),
    cutoffs: VerifiedCutoffs = Depends(get_alert_cutoffs),
    limit: int = Query(
        default=DEFAULT_ALERT_LIMIT,
        ge=1,
        le=MAX_ALERT_LIMIT,
        description=(
            "Max rows per section. Each section is capped independently; the "
            "response flags which ones were truncated."
        ),
    ),
) -> GetNetworkAlertsResponse:
    data = await network_alerts_service.get_network_alerts_for_observer(
        observer=observer,
        cutoffs=cutoffs,
        limit=limit,
    )
    return GetNetworkAlertsResponse(data=data)
