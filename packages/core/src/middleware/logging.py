import json
import logging
import re
import sys
import time
import uuid

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request

from src.observability.context import bind_request_id, get_request_id, reset_request_id
from src.observability.metrics import core_metrics
from src.observability.redaction import redact

_access_logger = logging.getLogger("trinity.access")

_http_requests_total = core_metrics.counter(
    "http_requests_total", "HTTP requests, labeled by method/path/status.",
)
_http_request_duration_ms = core_metrics.histogram(
    "http_request_duration_ms", "HTTP request duration, labeled by method/path.",
)
_http_errors_total = core_metrics.counter(
    "http_errors_total", "HTTP responses with status >= 500, labeled by method/path.",
)
_http_rejections_total = core_metrics.counter(
    "http_rejections_total", "HTTP 429 responses, labeled by method/path.",
)

# Route templates (e.g. "/sessions/{session_id}/chat") come from
# request.scope["route"] when available — Starlette's Router sets it on
# the same scope dict `call_next()` was given, in place, before the
# request reaches the endpoint, so it's already there once `call_next()`
# returns. Falls back to this regex (collapsing UUID-looking segments to
# ":id") for the few paths where no route matched at all (404s) — either
# way, this keeps the metrics label space bounded regardless of how many
# distinct session/user ids this process ever sees, rather than growing
# one label combination per concrete URL forever.
_UUID_SEGMENT = re.compile(
    r"/[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}"
)


def _route_template(request: Request) -> str:
    route = request.scope.get("route")
    if route is not None and getattr(route, "path", None):
        return route.path
    return _UUID_SEGMENT.sub("/:id", request.url.path)


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        log: dict = {
            "level": record.levelname,
            "logger": record.name,
            "message": redact(record.getMessage()),
            "time": self.formatTime(record, "%Y-%m-%dT%H:%M:%S"),
        }
        request_id = getattr(record, "request_id", None)
        if request_id:
            log["request_id"] = request_id
        if record.exc_info:
            log["exception"] = redact(self.formatException(record.exc_info))
        return json.dumps(log, ensure_ascii=False)


class RequestIdLogFilter(logging.Filter):
    """Stamps the current request's correlation ID (Etapa 5G) onto every
    LogRecord that reaches the root handler — including records from
    trinity_router's own loggers, since Python's logging propagates child
    records up to the root handler regardless of which package emitted
    them. See src/observability/context.py's module docstring for the
    full propagation argument (contextvars + asyncio.to_thread)."""

    def filter(self, record: logging.LogRecord) -> bool:
        record.request_id = get_request_id()
        return True


def setup_logging(debug: bool = False) -> None:
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(JsonFormatter())
    handler.addFilter(RequestIdLogFilter())

    root = logging.getLogger()
    root.setLevel(logging.DEBUG if debug else logging.INFO)
    root.handlers.clear()
    root.addHandler(handler)

    logging.getLogger("sqlalchemy.engine").setLevel(logging.WARNING)
    logging.getLogger("uvicorn.access").setLevel(logging.WARNING)
    logging.getLogger("litellm").setLevel(logging.WARNING)


class RequestLoggingMiddleware(BaseHTTPMiddleware):
    """Access logging (unchanged behavior) + Etapa 5G additions: binds a
    per-request correlation ID for the duration of the request (used by
    RequestIdLogFilter above) and records basic HTTP metrics
    (requests/duration/errors/rejections) into core_metrics.

    Trust model for an inbound X-Request-ID: same as sessions.py's
    _client_ip() for X-Forwarded-For — Railway (this project's only
    deployment target) is the sole intermediate hop that can set request
    headers for traffic reaching this process, so an inbound
    X-Request-ID is trusted as an upstream-assigned correlation id when
    present, and a fresh uuid4 is generated otherwise (always the case in
    local/dev runs with no proxy in front).
    """

    async def dispatch(self, request: Request, call_next):
        request_id = request.headers.get("x-request-id") or str(uuid.uuid4())
        token = bind_request_id(request_id)
        start = time.perf_counter()
        try:
            response = await call_next(request)
        finally:
            reset_request_id(token)
        duration_ms = round((time.perf_counter() - start) * 1000, 1)

        path = _route_template(request)
        _http_requests_total.inc(method=request.method, path=path, status=str(response.status_code))
        _http_request_duration_ms.observe(duration_ms, method=request.method, path=path)
        if response.status_code >= 500:
            _http_errors_total.inc(method=request.method, path=path)
        elif response.status_code == 429:
            _http_rejections_total.inc(method=request.method, path=path)

        response.headers["X-Request-ID"] = request_id
        _access_logger.info(
            "%s %s %d %.1fms",
            request.method,
            request.url.path,
            response.status_code,
            duration_ms,
        )
        return response
