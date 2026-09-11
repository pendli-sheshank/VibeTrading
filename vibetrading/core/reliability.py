from __future__ import annotations

import asyncio
import functools
import logging
import time
from collections.abc import Awaitable, Callable
from enum import Enum
from typing import TypeVar

from tenacity import (
    AsyncRetrying,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential_jitter,
)

from vibetrading.config import get_settings
from vibetrading.observability.alerts import send_alert
from vibetrading.observability.metrics import circuit_breaker_opens_total, circuit_kind

logger = logging.getLogger(__name__)

# Strong references for _record_circuit_open()'s fire-and-forget alert
# tasks -- see the comment at its call site below.
_pending_alert_tasks: set[asyncio.Task] = set()


def _record_circuit_open(name: str) -> None:
    circuit_breaker_opens_total.labels(kind=circuit_kind(name)).inc()
    if not get_settings().alert_webhook_url:
        # No webhook configured -- the WARNING log line CircuitBreaker.
        # on_failure() already emits (plus the metric above) is the whole
        # alert in this, the common/default, case. Skip scheduling a task
        # for send_alert()'s otherwise-redundant second log line.
        return
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        # Sync context (e.g. a plain unit test driving CircuitBreaker
        # directly) -- skip the async webhook call rather than crash for
        # lack of an event loop.
        return
    message = f"Circuit breaker '{name}' opened after repeated failures."
    # A Task with no reference held anywhere but the event loop's own
    # internal bookkeeping is eligible for GC mid-execution (a documented
    # asyncio pitfall -- see asyncio.create_task's own docs on this). Hold
    # a strong reference in this module-level set until it finishes, so a
    # real alert can't be silently dropped by a GC pass that happens to
    # land before the webhook POST inside send_alert() completes.
    task = asyncio.create_task(send_alert(message))
    _pending_alert_tasks.add(task)
    task.add_done_callback(_pending_alert_tasks.discard)


T = TypeVar("T")

DEFAULT_MAX_ATTEMPTS = 3
DEFAULT_FAILURE_THRESHOLD = 5
DEFAULT_COOLDOWN_SECONDS = 30.0


class CircuitBreakerOpenError(Exception):
    """Raised instead of even attempting a call while a circuit is open --
    the whole point of a breaker: fail fast instead of piling up more
    slow, doomed requests against a service that's already down."""


class _CircuitState(Enum):
    CLOSED = "closed"
    OPEN = "open"
    HALF_OPEN = "half_open"


class CircuitBreaker:
    """Per-integration (not per-call) failure tracker. CLOSED: calls go
    through normally. After failure_threshold consecutive failures, trips
    to OPEN: every call is rejected immediately (CircuitBreakerOpenError)
    without even attempting the underlying operation, for cooldown_seconds.
    After that, one call is let through as a trial (HALF_OPEN) -- success
    closes the circuit again, failure reopens it (and restarts the
    cooldown clock).
    """

    def __init__(
        self,
        name: str,
        failure_threshold: int = DEFAULT_FAILURE_THRESHOLD,
        cooldown_seconds: float = DEFAULT_COOLDOWN_SECONDS,
    ):
        self.name = name
        self.failure_threshold = failure_threshold
        self.cooldown_seconds = cooldown_seconds
        self._state = _CircuitState.CLOSED
        self._consecutive_failures = 0
        self._opened_at: float | None = None

    @property
    def state(self) -> str:
        self._maybe_half_open()
        return self._state.value

    def _maybe_half_open(self) -> None:
        eligible = self._state == _CircuitState.OPEN and self._opened_at is not None
        if eligible and time.monotonic() - self._opened_at >= self.cooldown_seconds:
            self._state = _CircuitState.HALF_OPEN

    def before_call(self) -> None:
        self._maybe_half_open()
        if self._state == _CircuitState.OPEN:
            raise CircuitBreakerOpenError(
                f"Circuit '{self.name}' is open (>= {self.failure_threshold} consecutive failures); "
                f"failing fast for up to {self.cooldown_seconds:.0f}s before the next trial call."
            )

    def on_success(self) -> None:
        if self._state != _CircuitState.CLOSED:
            logger.info("Circuit '%s' closed after a successful call.", self.name)
        self._consecutive_failures = 0
        self._state = _CircuitState.CLOSED
        self._opened_at = None

    def on_failure(self) -> None:
        self._consecutive_failures += 1
        was_half_open = self._state == _CircuitState.HALF_OPEN
        if was_half_open or self._consecutive_failures >= self.failure_threshold:
            if self._state != _CircuitState.OPEN:
                logger.warning(
                    "Circuit '%s' opened after %d consecutive failures.", self.name, self._consecutive_failures
                )
                _record_circuit_open(self.name)
            self._state = _CircuitState.OPEN
            self._opened_at = time.monotonic()


_circuit_breakers: dict[str, CircuitBreaker] = {}


def get_circuit_breaker(
    name: str,
    failure_threshold: int = DEFAULT_FAILURE_THRESHOLD,
    cooldown_seconds: float = DEFAULT_COOLDOWN_SECONDS,
) -> CircuitBreaker:
    """One breaker instance per name, process-wide -- callers pass a name
    scoped to the integration+account they're calling (e.g. a Dhan
    client_id, an LLM provider) so one tenant's/provider's outage doesn't
    trip a breaker shared with an unrelated one."""
    breaker = _circuit_breakers.get(name)
    if breaker is None:
        breaker = CircuitBreaker(name, failure_threshold=failure_threshold, cooldown_seconds=cooldown_seconds)
        _circuit_breakers[name] = breaker
    return breaker


def reset_all_circuit_breakers() -> None:
    """Test-only: clears every breaker's state between tests."""
    _circuit_breakers.clear()


def with_retry_and_circuit_breaker(
    circuit_name: str | Callable[..., str],
    retry_on: tuple[type[Exception], ...],
    max_attempts: int = DEFAULT_MAX_ATTEMPTS,
    failure_threshold: int = DEFAULT_FAILURE_THRESHOLD,
    cooldown_seconds: float = DEFAULT_COOLDOWN_SECONDS,
):
    """Decorator for an async method: retries up to max_attempts times
    (exponential backoff with jitter) on any exception type in retry_on,
    then records one success/failure against a named CircuitBreaker once
    all retries are exhausted (or the first attempt succeeds). While the
    breaker is open, calls fail fast with CircuitBreakerOpenError instead
    of even attempting the operation (or its retries).

    NEVER apply this to a non-idempotent write (order placement, above
    all) -- retrying a call whose first attempt may have already succeeded
    server-side, just with a lost/timed-out response, risks a duplicate
    side effect. This is only for reads and idempotent operations.

    circuit_name may be a plain string or a callable(*args, **kwargs) that
    derives one from the call's own arguments (e.g. a per-tenant/per-
    account key) -- evaluated fresh on every call, so it can depend on
    instance state (pass self.some_attribute-derived logic via a lambda).
    """

    def decorator(func: Callable[..., Awaitable[T]]) -> Callable[..., Awaitable[T]]:
        @functools.wraps(func)
        async def wrapper(*args, **kwargs) -> T:
            name = circuit_name(*args, **kwargs) if callable(circuit_name) else circuit_name
            breaker = get_circuit_breaker(name, failure_threshold=failure_threshold, cooldown_seconds=cooldown_seconds)
            breaker.before_call()

            try:
                async for attempt in AsyncRetrying(
                    stop=stop_after_attempt(max_attempts),
                    wait=wait_exponential_jitter(initial=0.5, max=10),
                    retry=retry_if_exception_type(retry_on),
                    reraise=True,
                ):
                    with attempt:
                        result = await func(*args, **kwargs)
            except Exception:
                breaker.on_failure()
                raise
            else:
                breaker.on_success()
                return result

        return wrapper

    return decorator
