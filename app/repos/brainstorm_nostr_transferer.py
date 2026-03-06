from sqlalchemy import insert, select
from app.core.database import execute_db_statement
from app.db_models import BrainstormNostrRelayTransfer
from sqlalchemy.ext.asyncio import AsyncSession as AsyncDBSession
from sqlalchemy.dialects.postgresql import insert


async def get_nostr_transfer_status_by_kind_from_db(
    db: AsyncDBSession, kind: int
) -> BrainstormNostrRelayTransfer | None:
    stmt = select(BrainstormNostrRelayTransfer).where(
        BrainstormNostrRelayTransfer.kind == kind
    )
    existing_data = await execute_db_statement(db, stmt, __name__)
    result: BrainstormNostrRelayTransfer | None = existing_data.scalar_one_or_none()
    return result


async def upsert_nostr_transfer_status_on_db(
    db: AsyncDBSession,
    kind: int,
    completed: bool,
    total_events: int,
    oldest: int,
    started_at: float,
) -> BrainstormNostrRelayTransfer | None:
    stmt = insert(BrainstormNostrRelayTransfer).values(
        kind=kind,
        completed=completed,
        oldest=oldest,
        events=total_events,
        started_at=started_at,
    )

    stmt = stmt.on_conflict_do_update(
        constraint="uq_brainstorm_nostr_relay_transfer_kind",
        set_={
            "completed": stmt.excluded.completed,
            "oldest": stmt.excluded.oldest,
            "events": stmt.excluded.events,
            "started_at": stmt.excluded.started_at,
        },
    )

    await db.execute(stmt)
    await db.commit()
