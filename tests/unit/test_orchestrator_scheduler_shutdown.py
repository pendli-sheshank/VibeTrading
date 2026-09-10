from __future__ import annotations

import asyncio

import pytest
from apscheduler.schedulers.base import STATE_PAUSED

from vibetrading.broker.mock_client import MockBrokerClient
from vibetrading.config import Settings
from vibetrading.orchestrator.scheduler import OrchestratorScheduler


@pytest.fixture
async def scheduler():
    sched = OrchestratorScheduler(broker=MockBrokerClient(seed=1), watchlist=[], settings=Settings(_env_file=None))
    sched.start()
    yield sched
    if sched.scheduler.running:
        sched.shutdown()


async def test_wait_until_idle_returns_immediately_when_nothing_running(scheduler):
    await asyncio.wait_for(scheduler.wait_until_idle(), timeout=0.1)


async def test_shutdown_gracefully_waits_for_in_flight_job_to_finish(scheduler):
    """Proves the manual job-tracking mechanism actually blocks
    shutdown_gracefully() until a running job finishes naturally -- this is
    the fix for AsyncIOExecutor.shutdown() not honoring wait=True (see the
    __init__ comment in scheduler.py)."""
    scheduler._job_started()
    finished = False

    async def finish_after_delay():
        nonlocal finished
        await asyncio.sleep(0.05)
        finished = True
        scheduler._job_finished()

    task = asyncio.create_task(finish_after_delay())
    await scheduler.shutdown_gracefully()

    assert finished is True
    # AsyncIOScheduler.shutdown() defers its actual state change to the next
    # loop iteration via call_soon_threadsafe (see its @run_in_event_loop
    # decorator) -- it isn't synchronous even though it's not an `async def`.
    # One tick is enough for it to land.
    await asyncio.sleep(0)
    assert scheduler.scheduler.running is False
    await task


async def test_shutdown_gracefully_pauses_before_waiting_so_no_new_jobs_start(scheduler):
    scheduler._job_started()
    task = asyncio.create_task(scheduler.shutdown_gracefully())
    await asyncio.sleep(0.01)

    assert scheduler.scheduler.state == STATE_PAUSED

    scheduler._job_finished()
    await task
