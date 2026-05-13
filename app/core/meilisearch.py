import httpx

from app.core.config import settings

NOSTR_PROFILES_INDEX = "nostr_profiles"

UPSERT_BATCH_SIZE = 5000


async def upsert_documents(
    index: str, documents: list[dict], primary_key: str = "pubkey"
):
    # Meilisearch PUT performs a partial update: fields included in the new
    # document overwrite existing values, fields absent are left untouched.
    # Documents are sent in chunks so a single huge payload doesn't stall
    # the request — each chunk becomes its own Meilisearch task.
    if not documents:
        return []

    url = f"{settings.meilisearch_url}/indexes/{index}/documents"
    headers = {
        "Authorization": f"Bearer {settings.meilisearch_master_key}",
        "Content-Type": "application/json",
    }
    params = {"primaryKey": primary_key}

    results = []
    async with httpx.AsyncClient(timeout=httpx.Timeout(60.0)) as client:
        for start in range(0, len(documents), UPSERT_BATCH_SIZE):
            chunk = documents[start : start + UPSERT_BATCH_SIZE]
            response = await client.put(
                url, headers=headers, json=chunk, params=params
            )
            response.raise_for_status()
            results.append(response.json())
    return results
