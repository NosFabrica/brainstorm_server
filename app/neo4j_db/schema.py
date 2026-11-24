"""The graph's schema: every constraint and index this server needs.

Neo4j has no migration tool here. The schema is declared once, below, and
`ensure_schema` creates whatever is missing; each statement is a no-op when its
constraint or index already exists. It runs when the ingest consumer starts, so
a rebuilt or restored store gets its schema back without a manual step.
"""
from dataclasses import dataclass

from neo4j import AsyncSession as AsyncNeoSession

from app.core.config import settings


@dataclass(frozen=True)
class SchemaItem:
    name: str
    cypher: str
    # Created only once User ids are assigned (USER_IDS_ENABLED).
    needs_user_ids: bool = False


SCHEMA: tuple[SchemaItem, ...] = (
    SchemaItem(
        "nostr_user_pubkey",
        "CREATE CONSTRAINT nostr_user_pubkey IF NOT EXISTS"
        " FOR (u:NostrUser) REQUIRE u.pubkey IS UNIQUE",
    ),
    SchemaItem(
        "nostr_user_user_id",
        "CREATE CONSTRAINT nostr_user_user_id IF NOT EXISTS"
        " FOR (u:NostrUser) REQUIRE u.user_id IS UNIQUE",
        needs_user_ids=True,
    ),
)


def schema_for(user_ids: bool) -> list[SchemaItem]:
    return [item for item in SCHEMA if user_ids or not item.needs_user_ids]


async def ensure_schema(
    session: AsyncNeoSession, *, user_ids: bool | None = None
) -> list[str]:
    """Create what is missing; returns the names ensured. `user_ids` defaults to
    the setting; a caller that is about to write ids passes True."""
    if user_ids is None:
        user_ids = settings.user_ids_enabled
    items = schema_for(user_ids)
    for item in items:
        await session.run(item.cypher)
    return [item.name for item in items]
