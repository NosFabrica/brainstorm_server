"""Data access for the billing tables."""

from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession as AsyncDBSession

from app.core.database import execute_db_statement
from app.db_models import BillingPlan, FlashWebhookEvent, UserSubscription


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


async def get_billing_plan_on_db(
    db: AsyncDBSession, *, flash_service_id: str, flash_plan_id: str
) -> BillingPlan | None:
    """The plan a Flash subscription belongs to. None if we don't map it."""
    statement = select(BillingPlan).where(
        BillingPlan.flash_service_id == flash_service_id,
        BillingPlan.flash_plan_id == flash_plan_id,
        BillingPlan.is_active.is_(True),
    )
    result = await execute_db_statement(db, statement, __name__)
    return result.scalar_one_or_none()


async def get_user_subscription_for_update_on_db(
    db: AsyncDBSession, pubkey: str
) -> UserSubscription | None:
    """Read one subscriber's record, locking the row for the transaction.

    Serialises concurrent deliveries for the same person, so the policy
    assignment and the record they write can't interleave into disagreement.
    """
    statement = (
        select(UserSubscription)
        .where(UserSubscription.pubkey == pubkey)
        .with_for_update()
    )
    result = await execute_db_statement(db, statement, __name__)
    return result.scalar_one_or_none()


async def upsert_user_subscription_on_db(
    db: AsyncDBSession,
    *,
    pubkey: str,
    flash_subscription_id: str,
    flash_subscriber_id: str | None,
    billing_plan_id: int,
    granted_scheduling_id: int | None,
    flash_status: str,
    current_period_start: datetime | None,
    current_period_end: datetime | None,
    next_billing_date: datetime | None,
    trial_end_date: datetime | None,
    cancel_effective_date: datetime | None,
) -> None:
    """Record what Flash says about one subscriber. One row per pubkey."""
    values = {
        "pubkey": pubkey,
        "flash_subscription_id": flash_subscription_id,
        "flash_subscriber_id": flash_subscriber_id,
        "billing_plan_id": billing_plan_id,
        "granted_scheduling_id": granted_scheduling_id,
        "flash_status": flash_status,
        "current_period_start": current_period_start,
        "current_period_end": current_period_end,
        "next_billing_date": next_billing_date,
        "trial_end_date": trial_end_date,
        "cancel_effective_date": cancel_effective_date,
        "last_synced_at": datetime.now(timezone.utc).replace(tzinfo=None),
        "last_sync_error": None,
    }
    statement = (
        pg_insert(UserSubscription)
        .values(**values)
        .on_conflict_do_update(
            index_elements=["pubkey"],
            set_={k: v for k, v in values.items() if k != "pubkey"},
        )
    )
    await execute_db_statement(db, statement, __name__)
