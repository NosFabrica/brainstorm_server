"""The compromised clear scans `scorecard` by Observee: it runs without the
server's statement_timeout and with a client cap above the engine's 180 s."""
import asyncio

from app.core.scorecard_database import SERVER_COMMAND_TIMEOUT_SECONDS
from app.repos import scorecard_repo


class _Driver:
    def __init__(self, log):
        self.log = log

    async def fetchval(self, query, *args, timeout=None):
        self.log.append(("fetchval", args, timeout))
        return 4


class _Raw:
    def __init__(self, log):
        self.driver_connection = _Driver(log)


class _Connection:
    def __init__(self, log):
        self.log = log

    async def get_raw_connection(self):
        return _Raw(self.log)


class _Session:
    def __init__(self):
        self.log = []

    async def execute(self, statement, params=None):
        self.log.append(("execute", str(statement)))

    async def connection(self):
        return _Connection(self.log)


def test_the_clear_lifts_the_statement_cap_then_deletes_with_a_longer_client_cap():
    db = _Session()
    assert asyncio.run(scorecard_repo.clear_scores_about_on_db(db, "ab" * 32)) == 4
    assert db.log[0] == ("execute", "SET LOCAL statement_timeout = 0")
    op, args, timeout = db.log[1]
    assert (op, args) == ("fetchval", ("ab" * 32,))
    assert timeout == scorecard_repo.CLEAR_SCORES_ABOUT_TIMEOUT_SECONDS
    assert timeout > SERVER_COMMAND_TIMEOUT_SECONDS
