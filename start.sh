#!/bin/bash
set -e
poetry run alembic upgrade head
poetry run uvicorn app.api:app --host 0.0.0.0 --port 8000