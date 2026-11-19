"""User id translation: what holds without a Scorecard database."""
import asyncio
from pathlib import Path

import pytest
from alembic.config import Config
from alembic.script import ScriptDirectory
from pydantic import ValidationError

from app.core.config import Settings
from app.repos import user_id_repo

ROOT = Path(__file__).resolve().parents[1]
VALID = "ab" * 32


def _no_database(monkeypatch):
    def _refuse():
        raise AssertionError("reached the database")

    monkeypatch.setattr(user_id_repo, "scorecard_session", _refuse)


@pytest.mark.parametrize(
    "bad",
    [
        "AB" * 32,
        "ab" * 31,
        "ab" * 33,
        "zz" * 32,
        "npub1" + "a" * 59,
        "",
        " " + "ab" * 32,
        "ab" * 32 + "\n",
    ],
)
def test_a_malformed_pubkey_never_reaches_the_table(monkeypatch, bad):
    _no_database(monkeypatch)
    with pytest.raises(ValueError):
        asyncio.run(user_id_repo.ids_for([VALID, bad]))
    with pytest.raises(ValueError):
        asyncio.run(user_id_repo.ids_for([bad], create=False))


def test_nothing_asked_is_nothing_read(monkeypatch):
    _no_database(monkeypatch)
    assert asyncio.run(user_id_repo.ids_for([])) == {}
    assert asyncio.run(user_id_repo.pubkeys_for([])) == {}


def test_ids_cannot_be_on_without_a_scorecard_database():
    with pytest.raises(ValidationError):
        Settings(user_ids_enabled=True, scorecard_db_url="")
    assert Settings(user_ids_enabled=False, scorecard_db_url="").user_ids_enabled is False


def test_the_scorecard_migrations_have_one_head():
    config = Config(str(ROOT / "alembic.ini"), ini_section="scorecard")
    config.set_main_option("script_location", str(ROOT / "alembic_scorecard"))
    heads = ScriptDirectory.from_config(config).get_heads()
    assert len(heads) == 1, f"migration heads: {heads}"
