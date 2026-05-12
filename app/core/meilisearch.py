import httpx

from app.core.config import settings

NOSTR_PROFILES_INDEX = "nostr_profiles"


async def upsert_documents(
    index: str, documents: list[dict], primary_key: str = "pubkey"
):
    # Meilisearch PUT performs a partial update: fields included in the new
    # document overwrite existing values, fields absent are left untouched.
    url = f"{settings.meilisearch_url}/indexes/{index}/documents"
    headers = {
        "Authorization": f"Bearer {settings.meilisearch_master_key}",
        "Content-Type": "application/json",
    }
    params = {"primaryKey": primary_key}

    async with httpx.AsyncClient() as client:
        response = await client.put(
            url, headers=headers, json=documents, params=params
        )
        response.raise_for_status()
        return response.json()
