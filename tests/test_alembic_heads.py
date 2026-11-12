"""The migration chain has one head. `start.sh` runs `alembic upgrade head` under
`set -e`, and with two heads that fails, so the container stops before uvicorn
starts. Two branches that each add a migration revising the same parent merge
cleanly in git and leave two heads, so this is checked without a database."""
from pathlib import Path

from alembic.config import Config
from alembic.script import ScriptDirectory

ROOT = Path(__file__).resolve().parents[1]


def test_the_migrations_have_one_head():
    config = Config(str(ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(ROOT / "alembic"))
    heads = ScriptDirectory.from_config(config).get_heads()
    assert len(heads) == 1, f"migration heads: {heads}"
