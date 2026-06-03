from app.core.config import settings

# Hardcoded observer pubkey used as the default trust-score perspective when
# settings.periodic_graperank_pubkey is not set.
DEFAULT_OBSERVER_PUBKEY = (
    "be7bf5de068c1d842ed34a7c270507ec940f5ea51671cfd062a95e9d09420d0a"
)


def default_observer_pubkey() -> str:
    """Observer perspective used for anonymous / unauthenticated requests."""
    return settings.periodic_graperank_pubkey or DEFAULT_OBSERVER_PUBKEY
