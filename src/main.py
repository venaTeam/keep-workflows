import asyncio
import logging
import os

import uvicorn
from contextlib import asynccontextmanager

from dotenv import find_dotenv, load_dotenv
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from starlette.middleware.cors import CORSMiddleware

load_dotenv(find_dotenv())

logger = logging.getLogger(__name__)

KEEP_VERSION = os.environ.get("KEEP_VERSION", "0.49.8")
HOST = os.environ.get("KEEP_WORKFLOWS_HOST", "0.0.0.0")
PORT = int(os.environ.get("KEEP_WORKFLOWS_PORT", "8082"))


# Keep a reference so the watcher task isn't garbage-collected.
_watcher_task = None


async def start_watcher_if_enabled():
    """Start the watcher loop (dismissal expiry + maintenance-window recovery).

    Mode flags are read at call time from src.api.config / src.common.consts.
    Returns the asyncio task for the non-Redis loop, None otherwise.
    """
    import src.api.config as api_config
    import src.common.consts as consts
    from src.common.event_management import process_watcher_task

    enabled = api_config.WATCHER or (
        api_config.MAINTENANCE_WINDOWS
        and consts.MAINTENANCE_WINDOW_ALERT_STRATEGY == "recover_previous_status"
    )
    if not enabled:
        logger.info("Watcher disabled, not starting")
        return None

    if consts.REDIS:
        from src.common.arq_pool import get_pool

        redis = await get_pool()
        job = await redis.enqueue_job(
            "async_process_watcher",
            _queue_name=consts.KEEP_ARQ_QUEUE_MAINTENANCE,
        )
        logger.info("Enqueued watcher job", extra={"job_id": job.job_id})
        return None

    task = asyncio.create_task(process_watcher_task.async_process_watcher())
    logger.info("Watcher task started (dismissal expiry + maintenance recovery)")
    return task


async def startup():
    """Runs on startup for each worker."""
    logger.info("Starting Keep Workflows service")
    # Import here to avoid circular imports
    from src.workflowmanager.workflowmanager import WorkflowManager

    workflow_manager = WorkflowManager.get_instance()
    await workflow_manager.start()
    logger.info("Workflow manager started")

    global _watcher_task
    try:
        _watcher_task = await start_watcher_if_enabled()
    except Exception:
        # Watcher is auxiliary — a failure to start it (e.g. Redis outage on
        # the arq path) must not take down the API worker.
        logger.exception("Failed to start the watcher")


async def shutdown():
    """Runs on shutdown for each worker."""
    logger.info("Shutting down Keep Workflows service")
    from src.workflowmanager.workflowmanager import WorkflowManager

    global _watcher_task
    if _watcher_task is not None:
        _watcher_task.cancel()
        _watcher_task = None

    try:
        wm = WorkflowManager.get_instance()
        wm.stop()
    except Exception:
        logger.exception("Error stopping workflow manager")
    logger.info("Keep Workflows shutdown complete")


@asynccontextmanager
async def lifespan(app: FastAPI):
    await startup()
    yield
    await shutdown()


def get_app() -> FastAPI:
    app = FastAPI(
        title="Keep Workflows API",
        description="Workflow engine service for Keep",
        version=KEEP_VERSION,
        lifespan=lifespan,
    )

    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    import importlib
    import sys

    if "src.routes.workflows" in sys.modules:
        importlib.reload(sys.modules["src.routes.workflows"])
    from src.routes.workflows import router as workflows_router

    app.include_router(workflows_router, prefix="/workflows", tags=["workflows"])

    from fastapi import Depends
    from src.identitymanager.authenticatedentity import AuthenticatedEntity
    from src.identitymanager.identitymanagerfactory import IdentityManagerFactory

    @app.get("/workflows/providers", tags=["providers"])
    @app.get("/providers", tags=["providers"])
    async def get_providers(
        authenticated_entity: AuthenticatedEntity = Depends(
            IdentityManagerFactory.get_auth_verifier(["read:providers"])
        ),
    ):
        from src.providers.providers_factory import ProvidersFactory
        from src.providers.providers_service import ProvidersService
        
        tenant_id = authenticated_entity.tenant_id
        providers = ProvidersFactory.get_all_providers()
        installed_providers = ProvidersService.get_installed_providers(tenant_id)
        linked_providers = ProvidersService.get_linked_providers(tenant_id)
        
        return {
            "providers": providers,
            "installed_providers": installed_providers,
            "linked_providers": linked_providers,
        }

    from src.routes.healthcheck import router as healthcheck_router

    app.include_router(healthcheck_router, prefix="/healthcheck", tags=["healthcheck"])

    @app.get("/", include_in_schema=False)
    async def root():
        return {"message": app.description, "version": KEEP_VERSION}

    @app.exception_handler(Exception)
    async def catch_exception(request: Request, exc: Exception):
        logger.error(f"An unhandled exception occurred: {exc}")
        return JSONResponse(
            status_code=500,
            content={
                "message": "An internal server error occurred.",
                "error_msg": str(exc),
            },
        )

    return app


app = get_app()

if __name__ == "__main__":
    uvicorn.run(
        "src.main:app",
        host=HOST,
        port=PORT,
        lifespan="on",
    )
