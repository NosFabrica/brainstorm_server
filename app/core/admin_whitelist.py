from nostr_sdk import PublicKey

from app.core.config import settings
from app.core.loggr import loggr

logger = loggr.get_logger(__name__)

_whitelisted_pubkeys: set[str] = set()


def _normalize_pubkey(raw: str) -> str:
    if raw.startswith("npub1"):
        return PublicKey.parse(raw).to_hex()
    return raw


def parse_pubkey_list(raw: str) -> set[str]:
    """Comma-separated hex or npub, normalised to hex. Shared with the billing list."""
    if not raw:
        return set()
    return {_normalize_pubkey(p.strip()) for p in raw.split(",") if p.strip()}


def init_admin_whitelist() -> None:
    global _whitelisted_pubkeys
    _whitelisted_pubkeys = parse_pubkey_list(settings.admin_whitelisted_pubkeys)

    if settings.admin_enabled:
        truncated = [
            PublicKey.parse(pk).to_bech32()[:16] + "..." for pk in _whitelisted_pubkeys
        ]
        logger.info(
            f"Admin routes ENABLED. Whitelisted pubkeys ({len(truncated)}): {truncated}"
        )
    else:
        logger.info("Admin routes DISABLED.")


def get_whitelisted_pubkeys() -> set[str]:
    return _whitelisted_pubkeys
