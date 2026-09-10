from __future__ import annotations

import pytest

from vibetrading.core.reliability import (
    CircuitBreaker,
    CircuitBreakerOpenError,
    get_circuit_breaker,
    reset_all_circuit_breakers,
    with_retry_and_circuit_breaker,
)


@pytest.fixture(autouse=True)
def _reset_breakers():
    reset_all_circuit_breakers()
    yield
    reset_all_circuit_breakers()


class TransientError(Exception):
    pass


class OtherError(Exception):
    pass


def test_breaker_starts_closed():
    breaker = CircuitBreaker("test", failure_threshold=3, cooldown_seconds=10)
    breaker.before_call()  # must not raise
    assert breaker.state == "closed"


def test_breaker_opens_after_threshold_consecutive_failures():
    breaker = CircuitBreaker("test", failure_threshold=3, cooldown_seconds=10)
    for _ in range(3):
        breaker.on_failure()

    assert breaker.state == "open"
    with pytest.raises(CircuitBreakerOpenError):
        breaker.before_call()


def test_a_success_before_the_threshold_resets_the_failure_count():
    breaker = CircuitBreaker("test", failure_threshold=3, cooldown_seconds=10)
    breaker.on_failure()
    breaker.on_failure()
    breaker.on_success()
    breaker.on_failure()
    breaker.on_failure()

    assert breaker.state == "closed"  # only 2 consecutive since the reset


def test_breaker_stays_open_until_the_cooldown_elapses():
    breaker = CircuitBreaker("test", failure_threshold=1, cooldown_seconds=10)
    breaker.on_failure()

    assert breaker.state == "open"
    with pytest.raises(CircuitBreakerOpenError):
        breaker.before_call()


def test_breaker_half_opens_after_cooldown_and_a_success_closes_it():
    # cooldown_seconds=0 -- the very next check is immediately eligible for
    # a half-open trial, without needing to sleep out a real cooldown.
    breaker = CircuitBreaker("test", failure_threshold=1, cooldown_seconds=0)
    breaker.on_failure()

    breaker.before_call()  # must not raise once half-open
    assert breaker.state == "half_open"

    breaker.on_success()
    assert breaker.state == "closed"


def test_a_failure_while_half_open_reopens_the_circuit():
    breaker = CircuitBreaker("test", failure_threshold=1, cooldown_seconds=0)
    breaker.on_failure()
    breaker.before_call()
    assert breaker.state == "half_open"

    # Reopening resets the cooldown clock -- give it a long one so the
    # very next check can't immediately re-flip to half-open again, which
    # would make "reopened" indistinguishable from "never left open."
    breaker.cooldown_seconds = 999
    breaker.on_failure()
    with pytest.raises(CircuitBreakerOpenError):
        breaker.before_call()


def test_get_circuit_breaker_returns_the_same_instance_for_the_same_name():
    a = get_circuit_breaker("shared")
    b = get_circuit_breaker("shared")
    assert a is b


def test_get_circuit_breaker_returns_independent_instances_for_different_names():
    a = get_circuit_breaker("one")
    b = get_circuit_breaker("two")
    assert a is not b


async def test_decorator_retries_on_the_configured_exception_and_eventually_succeeds():
    attempts = 0

    @with_retry_and_circuit_breaker("test", retry_on=(TransientError,), max_attempts=3)
    async def flaky():
        nonlocal attempts
        attempts += 1
        if attempts < 3:
            raise TransientError("not yet")
        return "ok"

    result = await flaky()
    assert result == "ok"
    assert attempts == 3


async def test_decorator_gives_up_after_max_attempts_and_records_a_failure():
    attempts = 0

    @with_retry_and_circuit_breaker("test", retry_on=(TransientError,), max_attempts=2, failure_threshold=5)
    async def always_fails():
        nonlocal attempts
        attempts += 1
        raise TransientError("nope")

    with pytest.raises(TransientError):
        await always_fails()

    assert attempts == 2
    assert get_circuit_breaker("test").state == "closed"  # below failure_threshold=5


async def test_decorator_does_not_retry_an_exception_outside_retry_on():
    attempts = 0

    @with_retry_and_circuit_breaker("test", retry_on=(TransientError,), max_attempts=3)
    async def wrong_error():
        nonlocal attempts
        attempts += 1
        raise OtherError("not retryable")

    with pytest.raises(OtherError):
        await wrong_error()

    assert attempts == 1


async def test_decorator_opens_the_circuit_after_repeated_call_level_failures():
    @with_retry_and_circuit_breaker("test", retry_on=(TransientError,), max_attempts=1, failure_threshold=2)
    async def always_fails():
        raise TransientError("nope")

    with pytest.raises(TransientError):
        await always_fails()
    with pytest.raises(TransientError):
        await always_fails()

    with pytest.raises(CircuitBreakerOpenError):
        await always_fails()  # third call fails fast -- the function itself never runs


async def test_decorator_circuit_name_can_depend_on_instance_state():
    class Client:
        def __init__(self, account_id: str):
            self.account_id = account_id

        @with_retry_and_circuit_breaker(
            lambda self: f"account:{self.account_id}", retry_on=(TransientError,), max_attempts=1, failure_threshold=1
        )
        async def call(self):
            raise TransientError("nope")

    client_a = Client("a")
    Client("b")  # never called -- proves its own circuit stays untouched

    with pytest.raises(TransientError):
        await client_a.call()

    assert get_circuit_breaker("account:a").state == "open"
    assert get_circuit_breaker("account:b").state == "closed"  # unaffected -- a different account
