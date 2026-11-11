import logging
import threading

import jwt
from fastapi import FastAPI, Request
from fastapi.testclient import TestClient

from src.api.middlewares import LoggingMiddleware
from src.common.logging import (
    LogContextFilter,
    reset_request_log_context,
    set_request_log_context,
    update_request_log_context,
)


class _CaptureHandler(logging.Handler):
    def __init__(self):
        super().__init__()
        self.records = []
        self.addFilter(LogContextFilter())

    def emit(self, record):
        self.records.append(record)


def _logger_with_capture(name):
    logger = logging.getLogger(name)
    logger.setLevel(logging.DEBUG)
    logger.propagate = False
    handler = _CaptureHandler()
    logger.addHandler(handler)
    return logger, handler


def test_request_logs_get_tenant_and_trace_id():
    logger, handler = _logger_with_capture("test.log_context.request")
    app = FastAPI()

    @app.get("/thing")
    def thing():
        logger.info("inside endpoint")
        return {}

    app.add_middleware(LoggingMiddleware)

    # Outer middleware stands in for TraceIDMiddleware.
    @app.middleware("http")
    async def set_trace_id(request: Request, call_next):
        request.state.trace_id = "trace-123"
        return await call_next(request)

    token = jwt.encode(
        {"keep_tenant_id": "tenant-abc"},
        "test-secret-key-long-enough-for-hs256",
        algorithm="HS256",
    )
    client = TestClient(app)
    response = client.get("/thing", headers={"Authorization": f"Bearer {token}"})

    assert response.status_code == 200
    record = next(r for r in handler.records if r.getMessage() == "inside endpoint")
    assert record.tenant_id == "tenant-abc"
    assert record.trace_id == "trace-123"


def test_auth_update_reaches_request_logs():
    logger, handler = _logger_with_capture("test.log_context.auth")
    token = set_request_log_context(tenant_id=None, trace_id="trace-1")
    try:
        update_request_log_context(tenant_id="tenant-from-api-key")
        logger.info("after auth")
    finally:
        reset_request_log_context(token)

    record = handler.records[-1]
    assert record.tenant_id == "tenant-from-api-key"
    assert record.trace_id == "trace-1"


def test_workflow_thread_logs_get_workflow_ids():
    logger, handler = _logger_with_capture("test.log_context.workflow")

    def run():
        thread = threading.current_thread()
        thread.workflow_id = "wf-1"
        thread.workflow_execution_id = "exec-1"
        thread.tenant_id = "tenant-wf"
        logger.info("workflow step")

    worker = threading.Thread(target=run)
    worker.start()
    worker.join()

    record = handler.records[-1]
    assert record.workflow_id == "wf-1"
    assert record.workflow_execution_id == "exec-1"
    assert record.tenant_id == "tenant-wf"


def test_explicit_extra_wins():
    logger, handler = _logger_with_capture("test.log_context.explicit")
    token = set_request_log_context(tenant_id="context-tenant", trace_id="context-trace")
    try:
        logger.info(
            "explicit", extra={"tenant_id": "explicit-tenant", "trace_id": None}
        )
    finally:
        reset_request_log_context(token)

    record = handler.records[-1]
    assert record.tenant_id == "explicit-tenant"
    assert record.trace_id is None


def test_no_context_does_not_crash():
    logger, handler = _logger_with_capture("test.log_context.none")
    logger.info("no context")

    record = handler.records[-1]
    assert record.getMessage() == "no context"
    for field in ("tenant_id", "trace_id", "workflow_id", "workflow_execution_id"):
        assert getattr(record, field, None) is None
