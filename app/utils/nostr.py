from nostr_sdk import Keys


def generate_random_nsec() -> str:
    keys = Keys.generate()
    return keys.secret_key().to_bech32()
