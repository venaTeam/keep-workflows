"""Settles PROMETHEUS_MULTIPROC_DIR, then re-exports the metric primitives.

**Modules that define metrics import their primitives from here, not from
prometheus_client.** That is the entire reason this file exists, and it is why
the one import below sits after executable code — an ordering that has to live
somewhere, isolated here instead of spread across every module that defines a
metric. Importing the names from one place also keeps the ordering safe from an
import sorter, which would otherwise hoist `prometheus_client` above a
first-party import and silently undo it.

Multiprocess mode is selected when prometheus_client first loads. Previously
that import sat above the env setup, so under gunicorn the metrics silently
fell back to single-process mode and never reached the dir /metrics scrapes.

(`src/main.py` still imports prometheus_client directly — it serves the
registry rather than defining metrics, so it is not part of this contract.)
"""

import logging
import os
import tempfile

logger = logging.getLogger(__name__)


def _resolve_prometheus_multiproc_dir() -> str:
    """Pick the multiprocess dir, refusing the two silent failure modes.

    A *set-but-empty* PROMETHEUS_MULTIPROC_DIR must not win over the default
    (``setdefault`` let it): prometheus_client joins the dir with
    ``counter_<pid>.db``, so an empty string writes metric mmap files into the
    process CWD — this exact bug got four ``*.db`` files committed to
    keep-event-handler's repo root. An uncreatable dir must not be silently
    swallowed for the same reason; fall back to the system temp dir and say
    so, never to the CWD.

    The default is workflows-specific so these files don't collide with other
    Keep services sharing the host (e.g. the gateway's ``/tmp/prometheus``,
    which it wipes on startup).
    """
    configured = (
        os.environ.get("PROMETHEUS_MULTIPROC_DIR") or "/tmp/prometheus_keep_workflows"
    )
    try:
        os.makedirs(configured, exist_ok=True)
        return configured
    except OSError:
        fallback = os.path.join(tempfile.gettempdir(), "prometheus_keep_workflows")
        logger.error(
            "PROMETHEUS_MULTIPROC_DIR %r cannot be created; using %r instead",
            configured,
            fallback,
        )
        os.makedirs(fallback, exist_ok=True)
        return fallback


os.environ["PROMETHEUS_MULTIPROC_DIR"] = _resolve_prometheus_multiproc_dir()

from prometheus_client import (  # noqa: E402  (see the module docstring)
    Counter,
    Gauge,
    Histogram,
    Summary,
)

__all__ = ["Counter", "Gauge", "Histogram", "Summary"]
