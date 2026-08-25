import asyncio
from contextlib import asynccontextmanager
import random
import string
import time

from app.message_queue_tasks.message_queue_consumer import (
    consume_job_started_messages,
    consume_messages,
    consume_nostr_upload_messages,
    consume_neo4j_write_messages,
    consume_strfry_plugin_messages,
    wait_until_graph_db_is_populated,
)
from app.message_queue_tasks.backfill_redis_relationships import (
    backfill_redis_relationships_if_needed,
)
from app.neo4j_db.driver import test_neo4j_driver
from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.middleware.gzip import GZipMiddleware
from fastapi_pagination import add_pagination

from app.core.config import settings
from app.core.loggr import loggr
from app.core.sql_admin_panel import add_sql_admin_panel
from app.core.flash import aclose as flash_aclose
from app.core.vespa import aclose as vespa_aclose
from app.routers.open_ranking.errors import install_ore_error_handlers
from app.routers.router import router as main_router
from app.utils.constants import DEPLOY_ENVIRONMENT_LOCAL
from app.services.flash_webhook_service import validate_flash_config
from app.services.nsec_encryption_service import bootstrap_keys
from app.nostr_event_transferer.nostr_event_transferer import (
    nostr_event_recent_transferer_cronjob,
    nostr_event_transferer,
)
from app.cronjobs.fail_stale_ongoing_brainstorm_requests import (
    fail_stale_ongoing_brainstorm_requests_cronjob,
)
from app.cronjobs.billing_sync import billing_sync_cronjob
from app.cronjobs.periodic_graperank_trigger import (
    periodic_graperank_trigger_cronjob,
)
from app.cronjobs.scheduler import scheduler_cronjob

from app.core.admin_whitelist import init_admin_whitelist
from app.core.billing_admin_whitelist import init_billing_admin_whitelist

logger = loggr.get_logger(__name__)

openapi_url = None
docs_url = None
redoc_url = None
swagger_ui_oauth2_redirect_url = None

if True:  # settings.deploy_environment == DEPLOY_ENVIRONMENT_LOCAL:
    openapi_url = "/openapi.json"
    docs_url = "/docs"
    redoc_url = "/redoc"
    swagger_ui_oauth2_redirect_url = "/docs/oauth2-redirect"


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Refuse to start half-configured: booting with payments enabled but no
    # credentials looks healthy, then fails as a webhook silently rejecting every
    # delivery while payments pile up unprocessed.
    validate_flash_config(
        enabled=settings.flash_enabled,
        api_key=settings.flash_api_key,
        webhook_secret=settings.flash_webhook_secret,
    )

    await bootstrap_keys()

    # initialize admin whitelist cache and log config
    init_admin_whitelist()
    init_billing_admin_whitelist()

    # test connectivity with Neo4j
    await test_neo4j_driver()

    # one-shot backwards-compat backfill: populate Redis follow/mute/report sets
    # from Neo4j on machines whose graph DB was populated before Redis was wired in.
    await backfill_redis_relationships_if_needed()

    consume_strfry_plugin_messages_task = asyncio.create_task(
        consume_strfry_plugin_messages()
    )

    if settings.perform_nostr_full_sync:
        # populate the STRFRY relay
        logger.info(
            "Populating your local Brainstorm Relay. Brainstorm is deactivated until it is finished"
        )
        await nostr_event_transferer()
        logger.info(
            "Finished populating your local Brainstorm Relay!! Populating your Graph DB..."
        )

        await wait_until_graph_db_is_populated()
        logger.info("Finished populating your Graph Database!! Enjoy Brainstorm!!")
    else:
        logger.info(
            "Skipping intial nostr relay full sync... if you want to do it, modify the env variables and restart."
        )
    # start the regular update cronjob task
    # regular_update_task = asyncio.create_task(nostr_event_recent_transferer_cronjob())

    # Start the listener task
    listener_task = asyncio.create_task(consume_messages())
    listener_nostr_upload_task = asyncio.create_task(consume_nostr_upload_messages())
    listener_neo4j_write_task = asyncio.create_task(consume_neo4j_write_messages())
    listener_ongoing_job_task = asyncio.create_task(consume_job_started_messages())
    fail_stale_ongoing_task = asyncio.create_task(
        fail_stale_ongoing_brainstorm_requests_cronjob()
    )
    periodic_graperank_task = asyncio.create_task(
        periodic_graperank_trigger_cronjob()
    )
    scheduler_task = asyncio.create_task(scheduler_cronjob())
    billing_sync_task = asyncio.create_task(billing_sync_cronjob())

    try:
        yield
    finally:
        # Graceful shutdown
        listener_task.cancel()
        listener_nostr_upload_task.cancel()
        listener_neo4j_write_task.cancel()
        listener_ongoing_job_task.cancel()
        consume_strfry_plugin_messages_task.cancel()
        fail_stale_ongoing_task.cancel()
        periodic_graperank_task.cancel()
        scheduler_task.cancel()
        billing_sync_task.cancel()
        # Awaited before the clients below are closed: a cancelled reconcile can
        # still be mid-GET, and closing the shared httpx client under it would
        # surface as a spurious Flash outage during every shutdown.
        await asyncio.gather(billing_sync_task, return_exceptions=True)
        # regular_update_task.cancel()
        await vespa_aclose()
        await flash_aclose()


app = FastAPI(
    title="brainstorm_api",
    description="",
    version="0.1.0",
    openapi_url=openapi_url,
    docs_url=docs_url,
    redoc_url=redoc_url,
    swagger_ui_oauth2_redirect_url=swagger_ui_oauth2_redirect_url,
    lifespan=lifespan,
)

origins = ["*"]
# if settings.deploy_environment != "LOCAL":
#     logger.info("Setting specific CORS origin...")
#     origins = [settings.frontend_url]

# Compress large JSON responses (e.g. /whitelisted, ~6.6MB of hex → ~3-4x smaller).
app.add_middleware(GZipMiddleware, minimum_size=1000)

logger.info("Allowing CORS...")
app.add_middleware(
    CORSMiddleware,
    allow_origins=origins,
    allow_credentials=False,  # TODO: REMOVE THIS ONCE NEEDED
    allow_methods=["*"],
    allow_headers=["*"],
    # ORE-00: browser clients must be able to read the Open Ranking error /
    # retry headers cross-origin.
    expose_headers=["X-Reason", "Retry-After"],
)

# Open Ranking error shape ({"error": ...} + X-Reason, PovComputing -> 202).
# Self-scoped to the ORE paths; every other route keeps FastAPI defaults.
install_ore_error_handlers(app)


@app.middleware(middleware_type="http")
async def log_requests(request: Request, call_next):
    idem = "".join(random.choices(string.ascii_uppercase + string.digits, k=6))
    logger.info(f"rid={idem} start request path={request.url.path}")
    start_time = time.time()

    response = await call_next(request)

    process_time = (time.time() - start_time) * 1000
    formatted_process_time = "{0:.2f}".format(process_time)
    logger.info(
        f"rid={idem} completed_in="
        f"{formatted_process_time}ms status_code={response.status_code}"
    )
    return response


@app.get(path="/health")
async def health_endpoint() -> int:
    return 1


app.include_router(
    router=main_router,
    prefix="",
)

if settings.deploy_environment == DEPLOY_ENVIRONMENT_LOCAL:
    add_sql_admin_panel(app)

add_pagination(app)
