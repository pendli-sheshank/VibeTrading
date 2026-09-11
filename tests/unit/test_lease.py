from __future__ import annotations

from datetime import UTC, datetime, timedelta

from vibetrading.orchestrator.lease import acquire_or_renew_lease, release_lease, verify_lease
from vibetrading.persistence.orm_models import TenantLeaseORM

TENANT_ID = 1


async def test_first_acquisition_creates_a_row_with_fencing_token_one(db_session):
    fencing_token = await acquire_or_renew_lease(db_session, TENANT_ID, "worker-a")
    await db_session.commit()

    assert fencing_token == 1
    lease = await db_session.get(TenantLeaseORM, TENANT_ID)
    assert lease.worker_id == "worker-a"


async def test_renewal_by_the_current_owner_keeps_the_same_fencing_token(db_session):
    first = await acquire_or_renew_lease(db_session, TENANT_ID, "worker-a")
    await db_session.commit()

    second = await acquire_or_renew_lease(db_session, TENANT_ID, "worker-a")
    await db_session.commit()

    assert first == second == 1


async def test_a_different_worker_cannot_acquire_a_live_lease(db_session):
    await acquire_or_renew_lease(db_session, TENANT_ID, "worker-a")
    await db_session.commit()

    result = await acquire_or_renew_lease(db_session, TENANT_ID, "worker-b")
    await db_session.commit()

    assert result is None


async def test_takeover_after_expiry_bumps_the_fencing_token(db_session):
    await acquire_or_renew_lease(db_session, TENANT_ID, "worker-a")
    await db_session.commit()

    # Simulate worker-a's lease having lapsed (it crashed and never renewed).
    lease = await db_session.get(TenantLeaseORM, TENANT_ID)
    lease.lease_expires_at = datetime.now(UTC) - timedelta(seconds=1)
    await db_session.commit()

    second_owner_token = await acquire_or_renew_lease(db_session, TENANT_ID, "worker-b")
    await db_session.commit()

    assert second_owner_token == 2
    lease = await db_session.get(TenantLeaseORM, TENANT_ID)
    assert lease.worker_id == "worker-b"


async def test_verify_lease_accepts_the_current_fencing_token(db_session):
    fencing_token = await acquire_or_renew_lease(db_session, TENANT_ID, "worker-a")
    await db_session.commit()

    assert await verify_lease(db_session, TENANT_ID, fencing_token) is True


async def test_verify_lease_rejects_a_stale_fencing_token_after_takeover(db_session):
    """The core safety proof: a worker holding an old fencing_token must be
    rejected the instant another worker has taken over -- this is what
    RiskEngine.approve_and_execute() checks before minting a token or
    placing an order."""
    stale_token = await acquire_or_renew_lease(db_session, TENANT_ID, "worker-a")
    await db_session.commit()

    lease = await db_session.get(TenantLeaseORM, TENANT_ID)
    lease.lease_expires_at = datetime.now(UTC) - timedelta(seconds=1)
    await db_session.commit()

    await acquire_or_renew_lease(db_session, TENANT_ID, "worker-b")
    await db_session.commit()

    assert await verify_lease(db_session, TENANT_ID, stale_token) is False


async def test_verify_lease_rejects_when_no_lease_row_exists(db_session):
    assert await verify_lease(db_session, TENANT_ID, 1) is False


async def test_verify_lease_rejects_an_expired_but_uncontested_lease(db_session):
    """Belt-and-suspenders beyond fencing-token equality: even if nobody
    else has raced in yet, a lease whose TTL lapsed should not keep passing
    verification forever."""
    fencing_token = await acquire_or_renew_lease(db_session, TENANT_ID, "worker-a")
    await db_session.commit()

    lease = await db_session.get(TenantLeaseORM, TENANT_ID)
    lease.lease_expires_at = datetime.now(UTC) - timedelta(seconds=1)
    await db_session.commit()

    assert await verify_lease(db_session, TENANT_ID, fencing_token) is False


async def test_release_lets_another_worker_acquire_immediately_without_waiting_out_the_ttl(db_session):
    await acquire_or_renew_lease(db_session, TENANT_ID, "worker-a")
    await db_session.commit()

    await release_lease(db_session, TENANT_ID, "worker-a")
    await db_session.commit()

    fencing_token = await acquire_or_renew_lease(db_session, TENANT_ID, "worker-b")
    await db_session.commit()

    assert fencing_token == 2  # bumped -- a new owner took over


async def test_release_is_a_no_op_for_a_lease_this_worker_no_longer_holds(db_session):
    await acquire_or_renew_lease(db_session, TENANT_ID, "worker-a")
    await db_session.commit()

    lease = await db_session.get(TenantLeaseORM, TENANT_ID)
    lease.lease_expires_at = datetime.now(UTC) - timedelta(seconds=1)
    await db_session.commit()

    await acquire_or_renew_lease(db_session, TENANT_ID, "worker-b")
    await db_session.commit()

    # worker-a's belated release must not clobber worker-b's ownership.
    await release_lease(db_session, TENANT_ID, "worker-a")
    await db_session.commit()

    lease = await db_session.get(TenantLeaseORM, TENANT_ID)
    assert lease.worker_id == "worker-b"


async def test_leases_are_independent_per_tenant(db_session):
    token_tenant_1 = await acquire_or_renew_lease(db_session, 1, "worker-a")
    token_tenant_2 = await acquire_or_renew_lease(db_session, 2, "worker-b")
    await db_session.commit()

    assert token_tenant_1 == 1
    assert token_tenant_2 == 1  # independent counters, not a shared sequence
