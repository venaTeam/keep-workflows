import logging
import src.common.logging
from src.common.core.config import starlette_config
from src.identitymanager.identitymanagerfactory import IdentityManagerTypes
from importlib import metadata

# We read AUTH_TYPE directly to avoid importing src.api.api which triggers a cascade of imports
# that might fail during early startup or in restricted environments.
# Using cast=str to ensure we always get a string, enforcing type if passing objects by mistake
AUTH_TYPE = starlette_config("AUTH_TYPE", default=IdentityManagerTypes.NOAUTH.value, cast=str).lower()
try:
    KEEP_VERSION = metadata.version("keep")
except Exception:
    KEEP_VERSION = starlette_config("KEEP_VERSION", default="unknown")

HOST = starlette_config("KEEP_HOST", default="0.0.0.0")
PORT = starlette_config("PORT", default=8080, cast=int)
SCHEDULER = starlette_config("SCHEDULER", default="true", cast=bool)
CONSUMER = starlette_config("CONSUMER", default="true", cast=bool)
TOPOLOGY = starlette_config("KEEP_TOPOLOGY_PROCESSOR", default="false", cast=bool)
WATCHER = starlette_config("WATCHER", default="false", cast=bool)
KEEP_DEBUG_TASKS = starlette_config("KEEP_DEBUG_TASKS", default="false", cast=bool)

KEEP_USE_LIMITER = starlette_config("KEEP_USE_LIMITER", default="false", cast=bool)
MAINTENANCE_WINDOWS = starlette_config("MAINTENANCE_WINDOWS", default="false", cast=bool)

KEEP_API_URL = starlette_config("KEEP_API_URL", default=None)
KEEP_METRICS = starlette_config("KEEP_METRICS", default="true", cast=bool)
KEEP_OTEL_ENABLED = starlette_config("KEEP_OTEL_ENABLED", default="true", cast=bool)
KEEP_WORKERS = starlette_config("KEEP_WORKERS", default=None, cast=int)
KEEP_LIMIT_CONCURRENCY = starlette_config("KEEP_LIMIT_CONCURRENCY", default=None, cast=int)
# Used for limiter default limits (defaults to 100/minute if env is not set)
# Note: This shares the env var name with Uvicorn concurrency but expects string format for SlowAPI
KEEP_LIMITER_DEFAULT_LIMIT = starlette_config("KEEP_LIMIT_CONCURRENCY", default="100/minute", cast=str)
KEEP_METRICS_LIMIT = starlette_config("KEEP_LIMIT_CONCURRENCY", default="10/minute", cast=str)

KEEP_EXTRACT_IDENTITY = starlette_config("KEEP_EXTRACT_IDENTITY", default="true", cast=bool)
KEEP_READ_ONLY = starlette_config("KEEP_READ_ONLY", default="false", cast=bool)
KEEP_PROVIDER_DISTRIBUTION_ENABLED = starlette_config("KEEP_PROVIDER_DISTRIBUTION_ENABLED", default="true", cast=bool)
KEEP_PLATFORM_URL = starlette_config("KEEP_PLATFORM_URL", default="https://platform.keephq.dev")

# CORS: comma-separated list of trusted browser origins allowed to make credentialed requests.
# Defaults to KEEP_PLATFORM_URL. Override with e.g.:
#   KEEP_CORS_TRUSTED_ORIGINS=https://app.example.com,https://staging.example.com
_cors_origins_raw = starlette_config(
    "KEEP_CORS_TRUSTED_ORIGINS",
    default=KEEP_PLATFORM_URL,
)
KEEP_CORS_TRUSTED_ORIGINS: list[str] = [
    o.strip() for o in _cors_origins_raw.split(",") if o.strip()
]




src.common.logging.setup_logging()
logger = logging.getLogger(__name__)



def on_starting(server=None):
    """This function is called by the gunicorn server when it starts"""
    from src.common.core.init import init_services
    from src.api.routes.dashboard import provision_dashboards
    
    init_services(auth_type=AUTH_TYPE, provision_dashboards_func=provision_dashboards)


def post_worker_init(worker):
    # We need to reinitialize logging in each worker because gunicorn forks the worker processes
    print("Init logging in worker")
    logging.getLogger().handlers = []  # noqa
    src.common.logging.setup_logging()  # noqa
    print("Logging initialized in worker")


post_worker_init = post_worker_init
