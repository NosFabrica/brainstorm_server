"""The NIP-89 `client` tag stamped on every event this server signs.

nostr-sdk only, no settings: `ta_signing` imports it inside worker processes.
"""

from nostr_sdk import Tag  # type: ignore

CLIENT_TAG = ["client", "Brainstorm"]

# Parsed once: Tag.parse costs ~35µs, ~10% of building and signing a TA, and
# a Tag is immutable, so every event can share this one.
_CLIENT_TAG = Tag.parse(CLIENT_TAG)


def client_tag() -> Tag:
    return _CLIENT_TAG
