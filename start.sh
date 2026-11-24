#!/bin/bash
set -e
poetry run alembic upgrade head
# Settings read env case-insensitively, so accept either spelling.
if [ -n "${SCORECARD_DB_URL:-${scorecard_db_url:-}}" ]; then
  poetry run alembic -n scorecard upgrade head
fi
poetry run uvicorn app.api:app --host 0.0.0.0 --port 8000
