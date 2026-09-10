from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker

from tests.conftest import TEST_DATABASE_URL, make_test_engine, reset_schema, seed_test_users
from vibetrading.orchestrator.lease import acquire_or_renew_lease
from vibetrading.persistence.orm_models import TenantLeaseORM

TENANT_ID = 1

pytestmark = pytest.mark.skipif(
    not TEST_DATABASE_URL.startswith("postgresql"),
    reason=(
        "Real concurrent-transaction races need a dialect with actual "
        "multi-connection locking (Postgres) -- SQLite's single shared "
        "connection (see make_test_engine()) serializes everything through "
        "Python-level scheduling, which wouldn't exercise the SELECT ... "
        "FOR UPDATE row lock this test is actually proving. Run with "
        "TEST_DATABASE_URL=postgresql+asyncpg://... to exercise it."
    ),
)


@pytest.fixture
async def session_factory():
    """A real Postgres engine, its own connection pool (not the single-
    shared-connection StaticPool make_test_engine() uses for SQLite) --
    two genuinely separate connections/transactions are the whole point of
    this test."""
    engine = make_test_engine()
    await reset_schema(engine)
    await seed_test_users(engine)
    yield async_sessionmaker(engine, expire_on_commit=False)
    await engine.dispose()


async def test_two_concurrent_workers_racing_for_a_brand_new_tenant_exactly_one_wins(session_factory):
    """Proves the INSERT-race path (no lease row exists yet) is safe: two
    workers reaching acquire_or_renew_lease for the same brand-new tenant
    at the same instant must not both believe they won."""

    async def attempt(worker_id: str) -> int | None:
        async with session_factory() as session:
            token = await acquire_or_renew_lease(session, TENANT_ID, worker_id)
            await session.commit()
            return token

    results = await asyncio.gather(attempt("worker-a"), attempt("worker-b"))
    winners = [r for r in results if r is not None]
    assert len(winners) == 1


async def test_two_concurrent_workers_racing_to_take_over_an_expired_lease_exactly_one_wins(session_factory):
    """Same proof, but for the takeover-after-expiry path (a real row
    already exists) -- the SELECT ... FOR UPDATE lock must serialize the
    two transactions rather than letting both read the same pre-takeover
    state and both decide they're the new owner."""
    async with session_factory() as session:
        await acquire_or_renew_lease(session, TENANT_ID, "worker-original")
        await session.commit()

    async with session_factory() as session:
        lease = await session.get(TenantLeaseORM, TENANT_ID)
        lease.lease_expires_at = datetime.now(UTC) - timedelta(seconds=1)
        await session.commit()

    async def attempt(worker_id: str) -> int | None:
        async with session_factory() as session:
            token = await acquire_or_renew_lease(session, TENANT_ID, worker_id)
            await session.commit()
            return token

    results = await asyncio.gather(attempt("worker-a"), attempt("worker-b"))
    winners = [r for r in results if r is not None]
    assert len(winners) == 1
    assert winners[0] == 2  # exactly one takeover happened, bumping the token once
