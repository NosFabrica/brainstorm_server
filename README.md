# Brainstorm API

This is the HTTP API for Brainstorm

## How to run the project.

- docker run --name my-postgres-db -e POSTGRES_PASSWORD=postgrespw -e POSTGRES_USER=postgres -e POSTGRES_DB=brainstorm-database -p 5432:5432 -d postgres:alpine3.20

- poetry install

- poetry shell

- cp env.example .env

- poetry run alembic upgrade head

- poetry run uvicorn app.api:app --host 0.0.0.0

## How to create a DB migration.

First, modify the DB files

- poetry run alembic revision --autogenerate -m "Your new modification"

## How to check the API project's documentation

Access the URL `http://0.0.0.0:8000/docs`

## How to interact with the DB with an Admin Panel

Access the URL `http://0.0.0.0:8000/admin`

## How to check if the project is fine

- poetry shell

- poe check_all (this is a custom command that runs many checkers)

## How to run everything with docker

- docker network create brainstorm-network
- docker-compose up
