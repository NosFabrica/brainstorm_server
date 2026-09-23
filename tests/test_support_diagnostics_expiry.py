"""The diagnostics snapshot expires; the ticket it belonged to does not.

The snapshot is the sharpest data support collects — the page the user was on
(profiles, events and hashtags, so *who* they were looking at) and recent
console error text, which on this client can carry pubkeys and relay URLs. It
is opt-out rather than opt-in, so most tickets have one, and it is the only
part of a ticket with no value once the ticket is done.
"""

from datetime import timedelta

import pytest
from sqlalchemy.dialects import postgresql

from app.repos.support_repo import build_expired_diagnostics_stmt


def _sql(stmt) -> str:
    return str(
        stmt.compile(
            dialect=postgresql.dialect(), compile_kwargs={"literal_binds": True}
        )
    )


def test_the_sweep_clears_only_the_snapshot():
    sql = _sql(build_expired_diagnostics_stmt(timedelta(days=30)))

    assert "UPDATE support_ticket SET diagnostics=NULL" in sql
    # The ticket, its messages and its events are not the sweep's business.
    for untouched in ("subject", "status", "notify_email", "closed_at", "pubkey"):
        assert f"{untouched}=" not in sql
    assert "DELETE" not in sql


def test_the_sweep_is_dated_from_filing_not_from_closing():
    # A snapshot describes the moment of filing and is stale well before the
    # window is out. Dating from closure would let a ticket nobody ever closes
    # keep its snapshot forever — the case this exists for.
    sql = _sql(build_expired_diagnostics_stmt(timedelta(days=30)))

    assert "support_ticket.created_at <" in sql
    assert "closed_at <" not in sql
    assert "status" not in sql


def test_a_snapshot_that_is_already_gone_is_not_rewritten():
    sql = _sql(build_expired_diagnostics_stmt(timedelta(days=30)))

    # Without this every sweep rewrites every old row forever.
    assert "diagnostics IS NOT NULL" in sql


@pytest.mark.parametrize("days", [1, 30, 365])
def test_the_window_is_what_it_is_given(days):
    from datetime import datetime, timezone

    before = datetime.now(timezone.utc).replace(tzinfo=None)
    sql = _sql(build_expired_diagnostics_stmt(timedelta(days=days)))
    after = datetime.now(timezone.utc).replace(tzinfo=None)

    cutoff_text = sql.split("support_ticket.created_at < '")[1].split("'")[0]
    cutoff = datetime.fromisoformat(cutoff_text)
    assert before - timedelta(days=days) <= cutoff <= after - timedelta(days=days)
