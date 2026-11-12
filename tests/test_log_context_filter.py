import logging
import threading

import jwt
from fastapi import FastAPI, Request
from fastapi.testclient import TestClient

from src.api.middlewares import LoggingMiddleware
from src.common.logging import (
    CustomJsonFormatter,
    LogContextFilter,
    WorkflowDBHandler,
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


def test_request_that_raises_logs_request_finished_500():
    middleware_logger = logging.getLogger("src.api.middlewares")
    original_level, original_propagate = (
        middleware_logger.level,
        middleware_logger.propagate,
    )
    _, handler = _logger_with_capture("src.api.middlewares")
    app = FastAPI()

    @app.get("/boom")
    def boom():
        raise RuntimeError("boom")

    app.add_middleware(LoggingMiddleware)

    client = TestClient(app, raise_server_exceptions=False)
    try:
        response = client.get("/boom")
    finally:
        middleware_logger.removeHandler(handler)
        middleware_logger.setLevel(original_level)
        middleware_logger.propagate = original_propagate

    assert response.status_code == 500
    finished = [r for r in handler.records if r.getMessage() == "Request finished"]
    assert len(finished) == 1
    assert finished[0].levelno == logging.ERROR
    assert finished[0].status_code == 500
    assert finished[0].method == "GET"
    assert finished[0].path == "/boom"
    assert finished[0].exc_info is None


def test_workflow_event_goes_to_db_context_not_json_logs():
    record = logging.LogRecord("test", logging.INFO, __file__, 1, "msg", None, None)
    record._event = {"name": "server-is-down"}
    record.context = {"step_id": "step-1"}

    assert "server-is-down" not in CustomJsonFormatter("%(message)s").format(record)
    entry = WorkflowDBHandler._to_log_entry(record)
    assert entry["context"] == {
        "step_id": "step-1",
        "event": {"name": "server-is-down"},
    }
