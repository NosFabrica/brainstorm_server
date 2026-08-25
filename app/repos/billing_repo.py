"""Data access for the billing tables.

Slice 01 covers the webhook inbox only; subscription and plan access land with
the entitlement work.
"""

from datetime import datetime

from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession as AsyncDBSession

from app.core.database import execute_db_statement
from app.db_models import FlashWebhookEvent


async def insert_flash_webhook_event_on_db(
    db: AsyncDBSession,
    *,
    event: str,
    delivery_timestamp: int,
    event_timestamp: datetime | None,
    subscription_id: str | None,
    payload: dict,
    dedupe_key: str,
) -> bool:
    """Record one delivery. Returns False if we already had it.

    ON CONFLICT DO NOTHING rather than catching IntegrityError: a raised
    constraint violation poisons the transaction, and this runs on the path that
    must still commit and answer 200 so Flash stops retrying.
    """
    statement = (
        pg_insert(FlashWebhookEvent)
        .values(
            event=event,
            delivery_timestamp=delivery_timestamp,
            event_timestamp=event_timestamp,
            subscription_id=subscription_id,
            payload=payload,
            dedupe_key=dedupe_key,
        )
        .on_conflict_do_nothing(index_elements=["dedupe_key"])
        .returning(FlashWebhookEvent.id)
    )
    result = await execute_db_statement(db, statement, __name__)
    return result.scalar_one_or_none() is not None
