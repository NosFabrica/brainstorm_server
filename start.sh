#!/bin/bash
set -e
poetry run alembic upgrade head
# The public API. With OBSERVER_RUNNER_TOKEN set, the lifespan also serves the
# internal API on OBSERVER_RUNNER_PORT (app/internal_api.py); never route that
# port through the ingress.
poetry run uvicorn app.api:app --host 0.0.0.0 --port 8000
