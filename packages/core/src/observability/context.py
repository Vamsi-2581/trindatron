"""Request correlation ID — Etapa 5G (Observability).

A single `contextvars.ContextVar` holds the current request's correlation
ID for the lifetime of that request's asyncio task (and any child task it
spawns via `asyncio.to_thread()`, which copies the current context — see
https://docs.python.org/3/library/contextvars.html and
asyncio.to_thread()'s own documented context-propagation guarantee).

Deliberately NOT a request-scoped object threaded explicitly through every
function call (chat_service.py, agent_service.py, entitlement_service.py,
trinity_router's ModelRouter, ...) — that would touch dozens of already-
closed call sites across two packages for a purely observational concern.
Instead, `RequestLoggingMiddleware` (src/middleware/logging.py) binds this
ContextVar once per HTTP request, and `RequestIdLogFilter` (same module)
reads it back at log-emit time and stamps it onto every LogRecord that
reaches the root handler — including records from `trinity_router.*`
loggers, since Python's logging propagates child-logger records up to the
root handler regardless of which package emitted them. This is what makes
a request_id show up in Core's own logs AND in the Model Router's
per-attempt audit log (audit.py) for the very same request, without either
package needing to know about the other's correlation mechanism.
"""
from __future__ import annotations

import contextvars

_request_id_var: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "trinity_request_id", default=None,
)


def get_request_id() -> str | None:
    """Current request's correlation ID, or None outside any request
    (startup code, a background task never bound to a request, tests that
    call a service function directly without going through the
    middleware)."""
    return _request_id_var.get()


def bind_request_id(request_id: str) -> contextvars.Token:
    """Bind `request_id` as current for this context. Returns a token for
    `reset_request_id()` — callers (the middleware) MUST reset it when the
    request ends, so a value never leaks into an unrelated later request
    reusing the same worker/thread."""
    return _request_id_var.set(request_id)


def reset_request_id(token: contextvars.Token) -> None:
    _request_id_var.reset(token)
