"""The internal API: the Observer print worker's `/internal/observerRunner/*`,
served by its own ASGI app on its own port.

It decrypts and signs as any owner's Assistant, so it shares nothing with the
public app: not its port (8000, the ingress backend), not its CORS `*`, not its
OpenAPI document, not its routes (the public app answers 404 for `/internal/`
before routing). The worker reaches `OBSERVER_RUNNER_PORT` directly on the
internal network; no Service the ingress points at may expose it.

`start_internal_api` runs it in this process, from the public app's lifespan,
on the same event loop, so it shares the database engine and the nsec keys
`bootstrap_keys` loaded. It starts only when `OBSERVER_RUNNER_TOKEN` is set and
long enough, and the port isn't the public one; otherwise it logs why and
nothing listens. Every request is checked for the token before routing
(`guard.RequireRunnerToken`).
"""
from __future__ import annotations

import asyncio
import contextlib
from collections.abc import Generator
from dataclasses import dataclass

import uvicorn
from fastapi import FastAPI

from app.core.config import settings
from app.core.loggr import loggr
from app.routers.observer_runner.guard import RequireRunnerToken, token_refusal
from app.routers.observer_runner.router import router as observer_runner_router

logger = loggr.get_logger(__name__)

# The public app's port: `start.sh`'s `--port`, the ingress backend's.
PUBLIC_API_PORT = 8000
# How long shutdown waits for in-flight worker calls.
STOP_TIMEOUT_SECONDS = 10

internal_app = FastAPI(
    title="brainstorm_internal",
    openapi_url=None,
    docs_url=None,
    redoc_url=None,
    # A trailing slash is a 404, not a redirect that says the route is here.
    redirect_slashes=False,
)
internal_app.include_router(
    router=observer_runner_router, prefix="/internal/observerRunner"
)
internal_app.add_middleware(RequireRunnerToken)


def internal_api_refusal() -> str | None:
    """Why the internal API must not start, or None when it may."""
    why = token_refusal()
    if why is not None:
        return why
    if settings.observer_runner_port == PUBLIC_API_PORT:
        return (
            f"OBSERVER_RUNNER_PORT is {PUBLIC_API_PORT}, the public API's port; "
            "the internal API needs a port of its own"
        )
    return None


class _EmbeddedServer(uvicorn.Server):
    """A uvicorn server that leaves signals to the public one, which owns the
    process and stops this one from its lifespan."""

    @contextlib.contextmanager
    def capture_signals(self) -> Generator[None, None, None]:
        yield


@dataclass
class InternalApi:
    server: uvicorn.Server
    task: asyncio.Task


async def _serve(server: uvicorn.Server) -> None:
    try:
        await server.serve()
    except SystemExit:
        # uvicorn exits the process when it can't bind; the public API stays up.
        logger.error(
            f"internal API not started: couldn't listen on port {server.config.port}"
        )


async def start_internal_api(
    host: str = "0.0.0.0", port: int | None = None
) -> InternalApi | None:
    """Serve `internal_app` beside the public app, or log why not and return
    None. `port` defaults to `OBSERVER_RUNNER_PORT`."""
    why = internal_api_refusal()
    if why is not None:
        if settings.observer_runner_token:
            logger.error(f"internal API not started: {why}")
        else:
            logger.info(f"internal API off: {why}")
        return None
    config = uvicorn.Config(
        internal_app,
        host=host,
        port=port if port is not None else settings.observer_runner_port,
        lifespan="off",
        # No websockets here.
        ws="none",
        # The process's logging is already configured; don't redo it.
        log_config=None,
        access_log=False,
    )
    server = _EmbeddedServer(config)
    task = asyncio.create_task(_serve(server))
    logger.info(f"internal API on port {config.port}")
    return InternalApi(server=server, task=task)


async def stop_internal_api(internal: InternalApi | None) -> None:
    if internal is None:
        return
    internal.server.should_exit = True
    try:
        await asyncio.wait_for(internal.task, STOP_TIMEOUT_SECONDS)
    except asyncio.TimeoutError:
        logger.warning("internal API didn't stop in time; cancelled")
