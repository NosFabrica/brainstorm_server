"""Data access for `flash_webhook_event` — the inbox, not a ledger. Never read
to decide whether someone is paid; that comes from Flash's API."""

from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import Integer, String, and_, case, cast, func, or_, select, update
from sqlalchemy.dialects.postgresql import array
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession as AsyncDBSession

from app.core.database import execute_db_statement
from app.db_models import BillingPlan, FlashWebhookEvent


async def insert_flash_webhook_event_on_db(
    db: AsyncDBSession,
    *,
    event: str,
    delivery_timestamp: int,
    event_timestamp: datetime | None,
    subscription_id: str | None,
    payload: dict,
    dedupe_key: str,
    claimed_at: datetime | None = None,
) -> int | None:
    """Record one delivery. Returns its id, or None if we already had it.

    `claimed_at` marks it as already being worked on. The webhook path passes it
    because it is about to do exactly that — without it the row looks abandoned
    from the moment it lands, and the recovery sweep would duplicate every
    delivery still in flight.

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
            processing_started_at=claimed_at,
            attempts=1 if claimed_at else 0,
        )
        .on_conflict_do_nothing(index_elements=["dedupe_key"])
        .returning(FlashWebhookEvent.id)
    )
    result = await execute_db_statement(db, statement, __name__)
    return result.scalar_one_or_none()



async def select_abandoned_webhook_events_on_db(
    db: AsyncDBSession,
    *,
    now: datetime,
    stale_after: timedelta,
    max_attempts: int,
    limit: int,
) -> list[FlashWebhookEvent]:
    """Events we acknowledged and then never finished.

    The staleness window is what separates "a worker has this" from "a worker
    died holding this" — without it the sweep would fight live processing.
    """
    statement = (
        select(FlashWebhookEvent)
        .where(
            FlashWebhookEvent.processed_at.is_(None),
            FlashWebhookEvent.attempts < max_attempts,
            or_(
                FlashWebhookEvent.processing_started_at.is_(None),
                FlashWebhookEvent.processing_started_at <= now - stale_after,
            ),
        )
        .order_by(FlashWebhookEvent.created_at.asc())
        .limit(limit)
    )
    result = await execute_db_statement(db, statement, __name__)
    return list(result.scalars().all())


async def claim_webhook_event_on_db(
    db: AsyncDBSession, event_id: int, *, now: datetime, stale_after: timedelta
) -> bool:
    """Take ownership of one event. False means someone else got there first.

    The WHERE clause re-checks what the select found, so the claim is decided by
    the database rather than by whoever read first — which is what makes replay
    exactly-once with several replicas running.
    """
    statement = (
        update(FlashWebhookEvent)
        .where(
            FlashWebhookEvent.id == event_id,
            FlashWebhookEvent.processed_at.is_(None),
            or_(
                FlashWebhookEvent.processing_started_at.is_(None),
                FlashWebhookEvent.processing_started_at <= now - stale_after,
            ),
        )
        .values(
            processing_started_at=now,
            attempts=FlashWebhookEvent.attempts + 1,
        )
        .returning(FlashWebhookEvent.id)
    )
    result = await execute_db_statement(db, statement, __name__)
    return result.scalar_one_or_none() is not None


async def mark_webhook_event_processed_on_db(
    db: AsyncDBSession, event_id: int, *, now: datetime
) -> None:
    statement = (
        update(FlashWebhookEvent)
        .where(FlashWebhookEvent.id == event_id)
        .values(processed_at=now, process_error=None)
    )
    await execute_db_statement(db, statement, __name__)


async def record_webhook_event_failure_on_db(
    db: AsyncDBSession, event_id: int, reason: str
) -> None:
    """Leaves processed_at null, so it comes back round once the claim goes stale."""
    statement = (
        update(FlashWebhookEvent)
        .where(FlashWebhookEvent.id == event_id)
        .values(process_error=reason)
    )
    await execute_db_statement(db, statement, __name__)


# The only personal data Flash sends us. Everything else in the payload —
# amounts, invoice ids, dates — is accounting, and outlives the retention window.
PERSONAL_PAYLOAD_FIELDS = ("email", "name", "about", "picture_url")


async def prune_webhook_payloads_on_db(
    db: AsyncDBSession, *, older_than: datetime
) -> int:
    """Redact personal data from old events, keeping everything else.

    Redacting rather than dropping the payload: retention is about personal
    data, and nulling the whole thing would also delete the amounts the
    accounting export reads — silently emptying history at the retention
    boundary. The row, its dedupe key and its audit trail were never at risk.
    """
    redacted: Any = FlashWebhookEvent.payload
    for field in PERSONAL_PAYLOAD_FIELDS:
        redacted = func.jsonb_set(
            redacted, f"{{data,{field}}}", func.to_jsonb(cast(None, String))
        )

    statement = (
        update(FlashWebhookEvent)
        .where(
            FlashWebhookEvent.created_at <= older_than,
            FlashWebhookEvent.payload.is_not(None),
            # Never touch an event still waiting to be applied: replay reads the
            # payload to find the subscriber.
            FlashWebhookEvent.processed_at.is_not(None),
            # Only rows that still carry something personal.
            FlashWebhookEvent.payload["data"].has_any(
                array(PERSONAL_PAYLOAD_FIELDS)
            ),
        )
        .values(payload=redacted)
    )
    result = await execute_db_statement(db, statement, __name__)
    return result.rowcount



async def select_unresolved_events_on_db(db: AsyncDBSession, *, limit: int) -> list:
    """Deliveries that named nobody we could match, so nothing was applied."""
    statement = (
        select(
            FlashWebhookEvent.id,
            FlashWebhookEvent.event,
            FlashWebhookEvent.created_at,
            FlashWebhookEvent.process_error,
        )
        .where(
            FlashWebhookEvent.processed_at.is_(None),
            FlashWebhookEvent.process_error.is_not(None),
        )
        .order_by(FlashWebhookEvent.created_at.desc())
        .limit(limit)
    )
    result = await execute_db_statement(db, statement, __name__)
    return list(result.all())


async def select_exhausted_events_on_db(
    db: AsyncDBSession, *, max_attempts: int, limit: int
) -> list:
    """Events that ran out of replay attempts and are now nobody's job."""
    statement = select(
        FlashWebhookEvent.id,
        FlashWebhookEvent.event,
        FlashWebhookEvent.attempts,
        FlashWebhookEvent.process_error,
    ).where(
        FlashWebhookEvent.processed_at.is_(None),
        FlashWebhookEvent.attempts >= max_attempts,
    ).limit(limit)
    result = await execute_db_statement(db, statement, __name__)
    return list(result.all())


async def select_payment_history_on_db(
    db: AsyncDBSession, *, since: datetime, until: datetime, limit: int
) -> list:
    """Payments, read out of the stored renewal events.

    Deliberately not a second ledger: Flash took the money and is authoritative
    about it, so this reads what Flash told us rather than keeping a parallel
    record that could disagree. Pruned events drop out — the payload is where
    the amount lives.

    `activated` covers the first charge (there is no renewal event for period
    1), but its payload carries no amount — so the plan's configured price
    stands in, from a join on the payload's service/plan ids. The `event`
    column says which of the two each row is.
    """
    payload = FlashWebhookEvent.payload["data"]
    renewed = FlashWebhookEvent.event == "subscription.renewed"
    statement = (
        select(
            func.coalesce(
                payload["paidAt"].astext, payload["activatedAt"].astext
            ).label("paid_at"),
            FlashWebhookEvent.event.label("event"),
            payload["externalRef"].astext.label("pubkey"),
            FlashWebhookEvent.subscription_id,
            payload["invoiceId"].astext.label("invoice_id"),
            case(
                (renewed, payload["amount"].astext.cast(Integer)),
                else_=BillingPlan.amount_minor,
            ).label("amount_minor"),
            case(
                (renewed, payload["currency"].astext),
                else_=BillingPlan.currency,
            ).label("currency"),
        )
        .outerjoin(
            BillingPlan,
            and_(
                BillingPlan.flash_service_id == payload["serviceId"].astext,
                BillingPlan.flash_plan_id == payload["planId"].astext,
            ),
        )
        .where(
            FlashWebhookEvent.event.in_(
                ("subscription.renewed", "subscription.activated")
            ),
            FlashWebhookEvent.payload.is_not(None),
            FlashWebhookEvent.created_at >= since,
            FlashWebhookEvent.created_at <= until,
        )
        .order_by(FlashWebhookEvent.created_at.desc())
        .limit(limit)
    )
    result = await execute_db_statement(db, statement, __name__)
    return list(result.all())
