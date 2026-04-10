#!/bin/bash
set -e
poetry run alembic upgrade head
exec $(poetry env info --path)/bin/python -m uvicorn app.api:app --host 0.0.0.0 --port 8000
