"""Timeouts on Scorecard database connections: the server, scripts, migrations."""
import ast
from pathlib import Path

import pytest
from alembic.config import Config
from alembic.script import ScriptDirectory

from app.core import scorecard_database as sdb

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS_ON_SCORECARDS = [
    p
    for p in sorted((ROOT / "scripts").glob("*.py"))
    if "app.core.scorecard_database" in p.read_text()
    or "app.repos.user_id_repo" in p.read_text()
    or "app.repos.scorecard_repo" in p.read_text()
]


@pytest.fixture
def engine_args(monkeypatch):
    seen = {}

    def fake_create(url, **kwargs):
        seen.update(kwargs)
        return object()

    monkeypatch.setattr(sdb, "create_async_engine", fake_create)
    monkeypatch.setattr(sdb.settings, "scorecard_db_url", "postgresql+asyncpg://x/y")
    monkeypatch.setattr(sdb, "_engine", None)
    monkeypatch.setattr(sdb, "_unbounded", False)
    return seen


def test_the_server_caps_statements_at_60s_inside_the_180s_client_cap(engine_args):
    sdb.scorecard_engine()
    assert engine_args["connect_args"] == {
        "command_timeout": 180.0,
        "server_settings": {"statement_timeout": "60000"},
    }


def test_a_script_runs_unbounded(engine_args):
    sdb.unbounded_statements()
    sdb.scorecard_engine()
    assert engine_args["connect_args"] == {
        "command_timeout": None,
        "server_settings": {"statement_timeout": "0"},
    }


def test_unbounded_after_the_engine_exists_refuses(engine_args):
    sdb.scorecard_engine()
    with pytest.raises(RuntimeError):
        sdb.unbounded_statements()


def test_migrations_take_no_statement_cap_and_a_lock_cap():
    assert sdb.MIGRATION_SERVER_SETTINGS == {
        "lock_timeout": "10s",
        "statement_timeout": "0",
    }


def test_migrations_build_their_own_engine_not_the_servers():
    env = (ROOT / "alembic_scorecard" / "env.py").read_text()
    assert "scorecard_engine" not in env and "scorecard_session" not in env
    assert "MIGRATION_SERVER_SETTINGS" in env


@pytest.mark.parametrize("script", SCRIPTS_ON_SCORECARDS, ids=lambda p: p.name)
def test_every_script_on_the_scorecard_database_runs_unbounded(script):
    calls = {
        n.func.id
        for n in ast.walk(ast.parse(script.read_text()))
        if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)
    }
    assert "unbounded_statements" in calls


def test_the_scorecard_migrations_have_one_head():
    config = Config(str(ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(ROOT / "alembic_scorecard"))
    heads = ScriptDirectory.from_config(config).get_heads()
    assert len(heads) == 1, f"scorecard migration heads: {heads}"
