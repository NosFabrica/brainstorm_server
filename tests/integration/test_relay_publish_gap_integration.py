"""A failed TA publish forces the observer's next run to publish in full, until a
full publish succeeds. Real Postgres; rows are scoped to a fresh observer."""

import asyncio

import pytest
from nostr_sdk import Keys
from sqlalchemy import delete

from app.core.database import db_session, engine
from app.db_models import BrainstormRequest, BrainstormRequestStatus
from app.repos.brainstorm_request_repo import relay_publish_gap_pending_on_db

pytestmark = pytest.mark.integration

S = BrainstormRequestStatus.SUCCESS.value
F = BrainstormRequestStatus.FAILURE.value


def _gap_after(runs: list[tuple[str, bool]]) -> bool:
    """runs: (ta_status, force_full_relay) in creation order; asks about the next run."""

    async def body():
        await engine.dispose()  # pooled connections may belong to an earlier test's loop
        observer = Keys.generate().public_key().to_hex()
        try:
            async with db_session() as db:
                for ta, full in runs:
                    db.add(
                        BrainstormRequest(
                            algorithm="graperank",
                            parameters=observer,
                            pubkey=observer,
                            status=S,
                            status_ta_publication=ta,
                            force_full_relay=full,
                        )
                    )
                current = BrainstormRequest(
                    algorithm="graperank", parameters=observer, pubkey=observer
                )
                db.add(current)
                await db.flush()
                gap = await relay_publish_gap_pending_on_db(
                    db, observer, current.private_id
                )
                await db.commit()
                return gap
        finally:
            async with db_session() as db:
                await db.execute(
                    delete(BrainstormRequest).where(
                        BrainstormRequest.pubkey == observer
                    )
                )
                await db.commit()
            await engine.dispose()

    return asyncio.run(body())


def test_no_history_means_no_gap():
    assert _gap_after([]) is False


def test_failed_publish_leaves_a_gap():
    assert _gap_after([(S, False), (F, False)]) is True


def test_successful_delta_after_failure_does_not_close_the_gap():
    assert _gap_after([(F, False), (S, False)]) is True


def test_successful_full_publish_closes_the_gap():
    assert _gap_after([(F, False), (S, True)]) is False


def test_failure_after_the_last_full_still_counts():
    assert _gap_after([(S, True), (F, False), (S, False)]) is True
