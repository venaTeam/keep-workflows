import logging
import os
import time

import jwt
from fastapi import Request
from starlette.middleware.base import BaseHTTPMiddleware

from src.common.logging import reset_request_log_context, set_request_log_context

logger = logging.getLogger(__name__)


def _extract_identity(request: Request, attribute="email") -> str:
    # Derive a cheap, non-DB identity for the request log. For API-key
    # requests we intentionally avoid a DB lookup here; the authenticated
    # tenant is recovered from request.state.tenant_id after authentication.
    try:
        authorization = request.headers.get("Authorization")
        if not authorization:
            return "anonymous"

        token = authorization.split(" ")[1]
        decoded_token = jwt.decode(token, options={"verify_signature": False})
        return decoded_token.get(attribute)
    except Exception:
        return "anonymous"


SENSITIVE_HEADERS = frozenset({"authorization", "cookie", "x-api-key"})


def _redact_headers(headers) -> dict:
    return {
        key: "[REDACTED]" if key.lower() in SENSITIVE_HEADERS else value
        for key, value in headers.items()
    }


PROBE_PATHS = frozenset({"/healthcheck", "/metrics", "/metrics/processing"})


class LoggingMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        identity = _extract_identity(request, attribute="keep_tenant_id")
        is_probe = request.url.path in PROBE_PATHS

        # for debugging purposes, log the payload
        if os.environ.get("LOG_AUTH_PAYLOAD", "false") == "true":
            logger.debug(
                "Request headers",
                extra={"headers": _redact_headers(request.headers)},
            )

        start_time = time.time()
        request.state.tenant_id = identity
        log_context_token = set_request_log_context(
            tenant_id=identity if identity != "anonymous" else None,
            trace_id=getattr(request.state, "trace_id", None),
        )
        try:
            response = await call_next(request)

            end_time = time.time()
            identity = getattr(request.state, "tenant_id", identity)
            if not is_probe:
                logger.info(
                    "Request finished",
                    extra={
                        "method": request.method,
                        "path": request.url.path,
                        "status_code": response.status_code,
                        "duration_ms": int((end_time - start_time) * 1000),
                        "tenant_id": identity,
                        "trace_id": getattr(request.state, "trace_id", None),
                    },
                )
            return response
        finally:
            reset_request_log_context(log_context_token)
