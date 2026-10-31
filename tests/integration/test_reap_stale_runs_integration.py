"""Stale in-pipeline runs are reaped whatever stage they stalled in, and a reaped run
stays failed when its worker reports late. Real Postgres; asserts only on rows
scoped to a fresh observer (the sweep itself is global, as the cronjob is)."""

import asyncio
from datetime import timedelta

import pytest
from nostr_sdk import Keys
from sqlalchemy import delete, select

from app.core.database import db_session, engine
from app.db_models import BrainstormRequest, BrainstormRequestStatus
from app.repos.brainstorm_request_repo import (
    fail_stale_in_pipeline_brainstorm_requests_on_db,
    update_brainstorm_request_result_by_id_on_db,
)
from app.utils.datetimes import utc_now

pytestmark = pytest.mark.integration

W = BrainstormRequestStatus.WAITING.value
O = BrainstormRequestStatus.ONGOING.value
S = BrainstormRequestStatus.SUCCESS.value
F = BrainstormRequestStatus.FAILURE.value
THRESHOLD = timedelta(hours=7)

# (status, ta, neo4j, hours since last update) → (status, ta, neo4j) after the sweep
CASES = {
    "never started": ((W, W, W, 8), (F, F, W)),
    "worker lost mid-run": ((O, W, W, 8), (F, F, W)),
    "publish hung": ((S, O, S, 8), (F, F, S)),
    "only neo4j left behind": ((S, S, W, 8), (S, S, W)),
    "already failed": ((F, F, W, 8), (F, F, W)),
    "still within the threshold": ((W, W, W, 1), (W, W, W)),
}


def _run(body):
    async def wrapped():
        await engine.dispose()
        observer = Keys.generate().public_key().to_hex()
        try:
            return await body(observer)
        finally:
            async with db_session() as db:
                await db.execute(
                    delete(BrainstormRequest).where(
                        BrainstormRequest.pubkey == observer
                    )
                )
                await db.commit()
            await engine.dispose()

    return asyncio.run(wrapped())


async def _add(db, observer, status, ta, neo4j, hours_ago) -> BrainstormRequest:
    row = BrainstormRequest(
        algorithm="graperank",
        parameters=observer,
        pubkey=observer,
        status=status,
        status_ta_publication=ta,
        status_internal_brainstorm_publication=neo4j,
        updated_at=utc_now() - timedelta(hours=hours_ago),
    )
    db.add(row)
    await db.flush()
    return row


async def _states(db, ids):
    rows = (
        await db.execute(
            select(BrainstormRequest).where(BrainstormRequest.private_id.in_(ids))
        )
    ).scalars()
    return {
        r.private_id: (
            r.status,
            r.status_ta_publication,
            r.status_internal_brainstorm_publication,
        )
        for r in rows
    }


def test_stale_runs_are_reaped_whatever_stage_they_stalled_in():
    async def body(observer):
        async with db_session() as db:
            ids = {
                name: (await _add(db, observer, *before)).private_id
                for name, (before, _) in CASES.items()
            }
            await db.commit()
        async with db_session() as db:
            reaped = await fail_stale_in_pipeline_brainstorm_requests_on_db(
                db, THRESHOLD
            )
            await db.commit()
        async with db_session() as db:
            states = await _states(db, list(ids.values()))
        return ids, reaped, states

    ids, reaped, states = _run(body)
    for name, (_, after) in CASES.items():
        assert states[ids[name]] == after, name
    assert ids["never started"] in reaped
    assert ids["only neo4j left behind"] not in reaped


def test_a_reaped_run_stays_failed_when_its_worker_reports_late():
    async def body(observer):
        async with db_session() as db:
            row_id = (await _add(db, observer, O, W, W, 8)).private_id
            await db.commit()
        async with db_session() as db:
            await fail_stale_in_pipeline_brainstorm_requests_on_db(db, THRESHOLD)
            await db.commit()
        for late in (BrainstormRequestStatus.ONGOING, BrainstormRequestStatus.SUCCESS):
            async with db_session() as db:
                await update_brainstorm_request_result_by_id_on_db(
                    db, brainstorm_request_id=row_id, count_values="{}", status=late
                )
                await db.commit()
        async with db_session() as db:
            return (await _states(db, [row_id]))[row_id]

    assert _run(body)[0] == F
