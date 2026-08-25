"""Who may see billing.

Separate from the general admin list so whoever answers "did my payment go
through?" does not thereby inherit the ability to rotate nsec encryption keys.
These views also carry subscriber email, which is the other reason to keep the
lists apart.

Falls back to the administrators when unset, so an existing deployment keeps
working without a new variable — but once a billing list is configured it is
authoritative, because a separate list that also admitted everyone else would
not be separate.
"""

from app.core.admin_whitelist import parse_pubkey_list
from app.core.config import settings
from app.core.loggr import loggr

logger = loggr.get_logger(__name__)

_billing_pubkeys: set[str] = set()


def init_billing_admin_whitelist() -> None:
    global _billing_pubkeys
    configured = settings.billing_admin_whitelisted_pubkeys
    _billing_pubkeys = parse_pubkey_list(
        configured or settings.admin_whitelisted_pubkeys
    )

    if configured:
        logger.info("Billing routes: %s authorised pubkey(s)", len(_billing_pubkeys))
    else:
        logger.info(
            "Billing routes: no separate list; falling back to the %s administrator(s)",
            len(_billing_pubkeys),
        )


def get_billing_pubkeys() -> set[str]:
    return _billing_pubkeys
