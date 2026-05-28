import asyncio
import logging
import os

import requests
import uvicorn
from contextlib import asynccontextmanager
from arq import ArqRedis
from dotenv import find_dotenv, load_dotenv
from fastapi import FastAPI, Request
from fastapi.middleware.gzip import GZipMiddleware
from fastapi.responses import JSONResponse
from prometheus_fastapi_instrumentator import Instrumentator
from slowapi import _rate_limit_exceeded_handler
from slowapi.errors import RateLimitExceeded
from slowapi.middleware import SlowAPIMiddleware
from starlette.middleware.cors import CORSMiddleware
from starlette_context import plugins
from starlette_context.middleware import RawContextMiddleware

import src.common.logging
import src.common.core.metrics
import src.common.observability
import src.common.utils.import_ee
from src.common.arq_pool import get_pool
from src.common.consts import (
    KEEP_ARQ_QUEUE_MAINTENANCE,
    MAINTENANCE_WINDOW_ALERT_STRATEGY,
    REDIS,
)

from src.common.core.db import dispose_session
from src.common.core.dependencies import SINGLE_TENANT_UUID
from src.api.core.limiter import limiter
from src.common.logging import CONFIG as logging_config
from src.api.middlewares import LoggingMiddleware
from src.api.routes.router_setup import setup_routers
from src.common.event_management import process_watcher_task
from src.event_subscriber.event_subscriber import EventSubscriber
from src.identitymanager.identitymanagerfactory import (
    IdentityManagerFactory,
    IdentityManagerTypes,
)
from src.topologies.topology_processor import TopologyProcessor

# load all providers into cache
from src.workflowmanager.workflowmanager import WorkflowManager

from src.api.config import (
    AUTH_TYPE,
    CONSUMER,
    HOST,
    KEEP_API_URL,
    KEEP_CORS_TRUSTED_ORIGINS,
    KEEP_DEBUG_TASKS,
    KEEP_LIMIT_CONCURRENCY,
    KEEP_METRICS,
    KEEP_OTEL_ENABLED,
    KEEP_USE_LIMITER,
    KEEP_VERSION,
    KEEP_WORKERS,
    MAINTENANCE_WINDOWS,
    PORT,
    SCHEDULER,
    TOPOLOGY,
    WATCHER,
)

load_dotenv(find_dotenv())
src.common.logging.setup_logging()
logger = logging.getLogger(__name__)


# Monkey patch requests to disable redirects
original_request = requests.Session.request


def no_redirect_request(self, method, url, **kwargs):
    kwargs["allow_redirects"] = False
    return original_request(self, method, url, **kwargs)


requests.Session.request = no_redirect_request


async def check_pending_tasks(background_tasks: set):
    while True:
        events_in_queue = len(background_tasks)
        logger.info(
            f"{events_in_queue} background tasks pending",
            extra={
                "pending_tasks": events_in_queue,
            },
        )
        await asyncio.sleep(1)


async def startup():
    """
    This runs for every worker on startup.
    Read more about lifespan here: https://fastapi.tiangolo.com/advanced/events/#lifespan
    """
    logger.info("Disope existing DB connections")
    # psycopg2.DatabaseError: error with status PGRES_TUPLES_OK and no message from the libpq
    # https://stackoverflow.com/questions/43944787/sqlalchemy-celery-with-scoped-session-error/54751019#54751019
    dispose_session()

    logger.info("Starting the services")

    # Start the scheduler
    if SCHEDULER:
        try:
            logger.info("Starting the scheduler")
            wf_manager = WorkflowManager.get_instance()
            await wf_manager.start()
            logger.info("Scheduler started successfully")
        except Exception:
            logger.exception("Failed to start the scheduler")

    # Start the consumer
    if CONSUMER:
        try:
            logger.info("Starting the consumer")
            event_subscriber = EventSubscriber.get_instance()
            # TODO: there is some "race condition" since if the consumer starts before the server,
            #       and start getting events, it will fail since the server is not ready yet
            #       we should add a "wait" here to make sure the server is ready
            await event_subscriber.start()
            logger.info("Consumer started successfully")
        except Exception:
            logger.exception("Failed to start the consumer")
    # Start the topology processor
    if TOPOLOGY:
        try:
            logger.info("Starting the topology processor")
            topology_processor = TopologyProcessor.get_instance()
            await topology_processor.start()
            logger.info("Topology processor started successfully")
        except Exception:
            logger.exception("Failed to start the topology processor")

    if WATCHER or (
        MAINTENANCE_WINDOWS
        and MAINTENANCE_WINDOW_ALERT_STRATEGY == "recover_previous_status"
    ):
        if REDIS:
            try:
                logger.info("Starting the watcher process")
                redis: ArqRedis = await get_pool()
                job = await redis.enqueue_job(
                    "async_process_watcher",
                    _queue_name=KEEP_ARQ_QUEUE_MAINTENANCE,
                )
                logger.info(
                    "Enqueued job",
                    extra={
                        "job_id": job.job_id,
                        "queue": KEEP_ARQ_QUEUE_MAINTENANCE,
                    },
                )
            except Exception:
                logger.exception("Failed to start the maintenance windows")
        else:
            asyncio.create_task(process_watcher_task.async_process_watcher())
            logger.info(
                "Added task",
                extra={
                    "task": "task",
                },
            )
    logger.info("Services started successfully")


async def shutdown():
    """
    This runs for every worker on shutdown.
    Read more about lifespan here: https://fastapi.tiangolo.com/advanced/events/#lifespan
    """
    logger.info("Shutting down Keep")
    if SCHEDULER:
        logger.info("Stopping the scheduler")
        wf_manager = WorkflowManager.get_instance()
        # stop the scheduler
        try:
            await wf_manager.stop()
        # in pytest, there could be race condition
        except TypeError:
            pass
        logger.info("Scheduler stopped successfully")
    if CONSUMER:
        logger.info("Stopping the consumer")
        event_subscriber = EventSubscriber.get_instance()
        try:
            await event_subscriber.stop()
        # in pytest, there could be race condition
        except TypeError:
            pass
        logger.info("Consumer stopped successfully")

    logger.info("Keep shutdown complete")


@asynccontextmanager
async def lifespan(app: FastAPI):
    """
    This runs for every worker on startup and shutdown.
    Read more about lifespan here: https://fastapi.tiangolo.com/advanced/events/#lifespan
    """
    app.state.limiter = limiter
    # create a set of background tasks
    background_tasks = set()
    # if debug tasks are enabled, create a task to check for pending tasks
    if KEEP_DEBUG_TASKS:
        logger.info("Starting background task to check for pending tasks")
        asyncio.create_task(check_pending_tasks(background_tasks))

    # Startup
    await startup()

    # yield the background tasks, this is available for the app to use in request context
    yield {"background_tasks": background_tasks}

    # Shutdown
    await shutdown()


def get_app(
    auth_type: IdentityManagerTypes = IdentityManagerTypes.NOAUTH.value,
) -> FastAPI:
    if not KEEP_API_URL:
        logger.info(
            "KEEP_API_URL is not set, setting it to default",
            extra={"keep_api_url": f"http://{HOST}:{PORT}"},
        )
        os.environ["KEEP_API_URL"] = f"http://{HOST}:{PORT}"

    logger.info(
        f"Starting Keep with {os.environ['KEEP_API_URL']} as URL and version {KEEP_VERSION}",
        extra={
            "keep_version": KEEP_VERSION,
            "keep_api_url": KEEP_API_URL,
        },
    )

    app = FastAPI(
        title="Keep API",
        description="Rest API powering https://platform.keephq.dev and friends 🏄‍♀️",
        version=KEEP_VERSION,
        lifespan=lifespan,
    )

    @app.get("/", include_in_schema=False)
    async def root():
        """
        App description and version.
        """
        return {"message": app.description, "version": KEEP_VERSION}

    app.add_middleware(RawContextMiddleware, plugins=(plugins.RequestIdPlugin(),))
    app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)
    app.add_middleware(
        GZipMiddleware, minimum_size=30 * 1024 * 1024
    )  # Approximately 30 MiB, https://cloud.google.com/run/quotas
    app.add_middleware(
        CORSMiddleware,
        allow_origins=KEEP_CORS_TRUSTED_ORIGINS,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )
    setup_routers(app)
    # if its single tenant with authentication, add signin endpoint
    logger.info(f"Starting Keep with authentication type: {AUTH_TYPE}")
    # If we run Keep with SINGLE_TENANT auth type, we want to add the signin endpoint
    identity_manager = IdentityManagerFactory.get_identity_manager(
        SINGLE_TENANT_UUID, None, AUTH_TYPE
    )
    # if any endpoints needed, add them on_start
    identity_manager.on_start(app)

    @app.exception_handler(Exception)
    async def catch_exception(request: Request, exc: Exception):
        logging.error(
            f"An unhandled exception occurred: {exc}, Trace ID: {request.state.trace_id}. Tenant ID: {request.state.tenant_id}"
        )
        return JSONResponse(
            status_code=500,
            content={
                "message": "An internal server error occurred.",
                "trace_id": request.state.trace_id,
                "error_msg": str(exc),
            },
        )

    app.add_middleware(LoggingMiddleware)
    if KEEP_USE_LIMITER:
        app.add_middleware(SlowAPIMiddleware)

    if KEEP_METRICS:
        instrumentator = Instrumentator(
            excluded_handlers=["/metrics", "/metrics/processing"],
            should_group_status_codes=False,
        )
        instrumentator.instrument(app=app, metric_namespace="keep")

    if KEEP_OTEL_ENABLED:
        src.common.observability.setup(app)

    return app


logging.basicConfig(level=logging.DEBUG)
logger = logging.getLogger(__name__)


def run(app: FastAPI):
    logger.info("Starting the uvicorn server")
    # call on starting to create the db and tables
    import src.api.config

    src.api.config.on_starting()

    uvicorn.run(
        "src.api.api:get_app",
        host=HOST,
        port=PORT,
        log_config=logging_config,
        lifespan="on",
        workers=KEEP_WORKERS,
        limit_concurrency=KEEP_LIMIT_CONCURRENCY,
    )
