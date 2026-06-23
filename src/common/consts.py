import os

from dotenv import find_dotenv, load_dotenv

from src.common.models.db.preset import PresetDto, StaticPresetsId

load_dotenv(find_dotenv())
RUNNING_IN_CLOUD_RUN = os.environ.get("K_SERVICE") is not None
PROVIDER_PULL_INTERVAL_MINUTE = int(
    os.environ.get("KEEP_PULL_INTERVAL", 10080)
)  # maximum once a week
STATIC_PRESETS = {
    "feed": PresetDto(
        id=StaticPresetsId.FEED_PRESET_ID.value,
        name="feed",
        options=[
            {"label": "CEL", "value": ""},
            {
                "label": "SQL",
                "value": {"sql": "", "params": {}},
            },
        ],
        created_by=None,
        is_private=False,
        is_noisy=False,
        should_do_noise_now=False,
        static=True,
        tags=[],
    )
}
MAINTENANCE_WINDOW_ALERT_STRATEGY = os.environ.get(
    "MAINTENANCE_WINDOW_STRATEGY", "default"
)  # recover_previous_status or default
WATCHER_LAPSED_TIME = int(os.environ.get("KEEP_WATCHER_LAPSED_TIME", 60))  # in seconds
###
# Set ARQ_TASK_POOL_TO_EXECUTE to "none", "all", "basic_processing" or "ai"
# to split the tasks between the workers.
###

KEEP_ARQ_TASK_POOL_ALL = "all"  # All arq workers enabled for this service
KEEP_ARQ_TASK_POOL_BASIC_PROCESSING = "basic_processing"  # Everything except AI
# Define queues for different task types
KEEP_ARQ_QUEUE_BASIC = "basic_processing"
KEEP_ARQ_QUEUE_WORKFLOWS = "workflows"
KEEP_ARQ_QUEUE_MAINTENANCE = "maintenance"

REDIS = os.environ.get("REDIS", "false") == "true"

if REDIS:
    KEEP_ARQ_TASK_POOL = os.environ.get("KEEP_ARQ_TASK_POOL", KEEP_ARQ_TASK_POOL_ALL)
else:
    KEEP_ARQ_TASK_POOL = os.environ.get("KEEP_ARQ_TASK_POOL", None)

OPENAI_MODEL_NAME = os.environ.get("OPENAI_MODEL_NAME", "gpt-4o-2024-08-06")


KEEP_CORRELATION_ENABLED = os.environ.get("KEEP_CORRELATION_ENABLED", "true") == "true"

MAX_PROCESSING_RETRIES = 3

# --- SC-04 consumer hardening (defaults reproduce the current baseline) ---
# Mirror of keep-event-handler's notify-offload + error-storm-guard knobs.
# keep-workflows has no Kafka consumer (it uses arq), so the KAFKA_* batch /
# retry vars are intentionally NOT mirrored here.

# SSE notify offload. Workers stays at 1 to preserve strict-FIFO delivery;
# raising it trades strict ordering for throughput (safe: the UI dedups
# poll-alerts by fingerprint, other events are idempotent).
SSE_NOTIFY_WORKERS = int(os.environ.get("SSE_NOTIFY_WORKERS", 1))
# Max pending notify items (distinct coalesce keys) before backpressure.
SSE_NOTIFY_MAX_PENDING = int(os.environ.get("SSE_NOTIFY_MAX_PENDING", 1000))
# Coalesce duplicate (tenant_id, event) notify signals while pending.
SSE_NOTIFY_COALESCE_ENABLED = (
    os.environ.get("SSE_NOTIFY_COALESCE_ENABLED", "true") == "true"
)

# Error-storm guard for AlertRaw(error=True) writes.
# TTL dup-suppression window in seconds.
KEEP_ERROR_STORM_WINDOW_SECONDS = int(
    os.environ.get("KEEP_ERROR_STORM_WINDOW_SECONDS", 60)
)
# Writes allowed per fingerprint per window.
KEEP_ERROR_STORM_MAX_PER_KEY = int(os.environ.get("KEEP_ERROR_STORM_MAX_PER_KEY", 1))
# LRU hard cap on the guard map so a high-cardinality burst can't grow memory.
KEEP_ERROR_GUARD_MAX_ENTRIES = int(
    os.environ.get("KEEP_ERROR_GUARD_MAX_ENTRIES", 10000)
)
