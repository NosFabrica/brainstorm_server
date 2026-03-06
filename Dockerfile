ARG PYTHON_VERSION=3.12-slim-bullseye

FROM python:${PYTHON_VERSION} AS python-base

ENV POETRY_VERSION=1.6.1
ENV POETRY_HOME=/opt/poetry
ENV POETRY_VENV=/opt/poetry-venv

ENV POETRY_CACHE_DIR=/opt/.cache

FROM python-base AS poetry-base

# Creating a virtual environment just for poetry and install it with pip
RUN python3 -m venv $POETRY_VENV \
    && $POETRY_VENV/bin/pip install -U pip setuptools \
    && $POETRY_VENV/bin/pip install poetry==${POETRY_VERSION}

FROM python-base AS example-app

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
RUN poetry install --no-interaction --no-cache --without dev

# Copy Application
COPY . .

CMD [ "poetry", "run", "alembic", "upgrade", "head", "&&" ,"poetry","run", "uvicorn" , "app.api:app", "--host", "0.0.0.0", "--port", "8000" ]