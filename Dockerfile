ARG PYTHON_VERSION=3.12-slim-bullseye

FROM python:${PYTHON_VERSION} AS python-base

ENV POETRY_VERSION=2.3.4
ENV POETRY_HOME=/opt/poetry
ENV POETRY_VENV=/opt/poetry-venv

ENV POETRY_CACHE_DIR=/opt/.cache

FROM python-base AS poetry-base

# Creating a virtual environment just for poetry and install it with pip
RUN python3 -m venv $POETRY_VENV \
    && $POETRY_VENV/bin/pip install -U pip setuptools \
    && $POETRY_VENV/bin/pip install poetry==${POETRY_VERSION}

FROM python-base AS example-app

# Vespa JVM feed-client (used for large batch feeds; see app/core/vespa.py
# _batch_upsert_feeder). The 13MB fat JAR comes from the official Vespa image;
# a headless JRE runs it. Both are gated behind VESPA_FEEDER_ENABLED at runtime.
COPY --from=vespaengine/vespa:latest \
    /opt/vespa/lib/jars/vespa-feed-client-cli-jar-with-dependencies.jar \
    /opt/vespa-feed-client.jar
# openjdk-17 explicitly — the JAR targets Java 17, and bullseye's default-jre is 11.
RUN apt-get update \
    && apt-get install -y --no-install-recommends openjdk-17-jre-headless \
    && rm -rf /var/lib/apt/lists/*

# Copy Poetry to app image
COPY --from=poetry-base ${POETRY_VENV} ${POETRY_VENV}

# Add Poetry to PATH
ENV PATH="${PATH}:${POETRY_VENV}/bin"

WORKDIR /app

# Copy Dependencies
COPY poetry.lock pyproject.toml ./

# [OPTIONAL] Validate the project is properly configured
# RUN poetry check

# Install Dependencies
RUN poetry install --no-interaction --no-cache --without dev --no-root

# Copy Application
COPY . .

COPY start.sh /app/start.sh
CMD ["/app/start.sh"]