from __future__ import annotations

import contextvars
import json
import logging
from datetime import UTC, datetime
from typing import Self

from vibetrading.config import get_settings

# Set for the duration of one scheduler job cycle (orchestrator/scheduler.py)
# or one HTTP/WebSocket request (auth/backend.py's current_*_user
# dependencies) via bind_tenant_id() below -- lets every log line emitted
# from deep inside a call chain (an agent, the risk engine, a broker call)
# be attributed to the tenant it's acting for, even though one process
# serves many tenants concurrently.
tenant_id_var: contextvars.ContextVar[int | None] = contextvars.ContextVar("tenant_id", default=None)

# Set per HTTP request (api/app.py's middleware) so every log line from
# one request -- across whatever dependencies/services it calls -- can be
# correlated, independent of tenant_id (present on some requests, e.g. an
# unauthenticated /login attempt, and absent on none).
request_id_var: contextvars.ContextVar[str | None] = contextvars.ContextVar("request_id", default=None)


class bind_tenant_id:
    """Context manager: sets tenant_id_var for its duration, restoring the
    previous value (usually None) on exit -- safe to nest, though nothing
    in this codebase currently does."""

    def __init__(self, tenant_id: int | None):
        self._tenant_id = tenant_id
        self._token: contextvars.Token | None = None

    def __enter__(self) -> Self:
        self._token = tenant_id_var.set(self._tenant_id)
        return self

    def __exit__(self, *exc_info: object) -> None:
        if self._token is not None:
            tenant_id_var.reset(self._token)


class bind_request_id:
    """Same pattern as bind_tenant_id, for request_id_var."""

    def __init__(self, request_id: str | None):
        self._request_id = request_id
        self._token: contextvars.Token | None = None

    def __enter__(self) -> Self:
        self._token = request_id_var.set(self._request_id)
        return self

    def __exit__(self, *exc_info: object) -> None:
        if self._token is not None:
            request_id_var.reset(self._token)


def _current_mode(tenant_id: int | None) -> str:
    """Live vs paper mode in every log line is a safety property, not
    cosmetics. Reads the acting tenant's own execution mode when one is
    bound (the common case -- almost everything that logs is doing so on
    behalf of a specific tenant); falls back to the process-wide infra
    default only when no tenant is bound at all (e.g. app startup)."""
    if tenant_id is not None:
        from vibetrading.settings.cache import get_tenant_settings

        return get_tenant_settings(tenant_id).vibetrading_execution_mode.value.upper()
    return get_settings().vibetrading_execution_mode.value.upper()


class ContextFilter(logging.Filter):
    """Stamps every log record with the currently-bound tenant_id/
    request_id and that tenant's execution mode."""

    def filter(self, record: logging.LogRecord) -> bool:
        tenant_id = tenant_id_var.get()
        record.tenant_id = tenant_id
        record.request_id = request_id_var.get()
        record.mode = _current_mode(tenant_id)
        return True


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload = {
            "timestamp": datetime.fromtimestamp(record.created, tz=UTC).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
            "mode": getattr(record, "mode", None),
            "tenant_id": getattr(record, "tenant_id", None),
            "request_id": getattr(record, "request_id", None),
        }
        if record.exc_info:
            payload["exc_info"] = self.formatException(record.exc_info)
        return json.dumps(payload, default=str)


def configure_logging() -> None:
    settings = get_settings()
    handler = logging.StreamHandler()
    handler.setFormatter(JsonFormatter())
    handler.addFilter(ContextFilter())

    root = logging.getLogger()
    root.setLevel(settings.log_level)
    root.handlers = [handler]
