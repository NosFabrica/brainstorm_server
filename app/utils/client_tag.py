"""The NIP-89 `client` tag stamped on every event this server signs.

nostr-sdk only, no settings: `ta_signing` imports it inside worker processes.
"""

from nostr_sdk import Tag  # type: ignore

CLIENT_TAG = ["client", "Brainstorm"]


def client_tag() -> Tag:
    return Tag.parse(CLIENT_TAG)
