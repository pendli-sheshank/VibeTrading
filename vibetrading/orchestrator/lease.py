from __future__ import annotations

from datetime import UTC, datetime, timedelta

from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from vibetrading.observability.metrics import lease_acquisition_failures_total
from vibetrading.persistence.orm_models import TenantLeaseORM

# TTL sized to roughly 3x the renewal interval, per the design's crash-
# handoff bound: a worker that dies mid-cycle without releasing its lease
# leaves that tenant unowned for at most one missed renewal window before
# another worker can take over.
DEFAULT_LEASE_TTL_SECONDS = 90
DEFAULT_RENEWAL_INTERVAL_SECONDS = 30


def _aware(dt: datetime) -> datetime:
    """SQLite round-trips a DateTime(timezone=True) value as naive (it has
    no real timezone-aware storage type, just a string) even though
    Postgres preserves it correctly -- the same quirk orchestrator/
    pipeline.py's _fresh_latest_output() already works around. The app
    only ever stores UTC, so a naive value read back is always UTC."""
    return dt if dt.tzinfo is not None else dt.replace(tzinfo=UTC)


async def acquire_or_renew_lease(
    session: AsyncSession,
    tenant_id: int,
    worker_id: str,
    ttl_seconds: int = DEFAULT_LEASE_TTL_SECONDS,
) -> int | None:
    """Attempts to become (or remain) the sole active owner of `tenant_id`'s
    trading loop. Returns the lease's current fencing_token on success,
    None if another worker already holds a live lease.

    Locks the row (SELECT ... FOR UPDATE, a no-op on SQLite -- fine there
    since SQLite has no real concurrent-transaction story to serialize
    against anyway) so two workers racing to acquire the same tenant at the
    same instant are strictly ordered rather than both believing they won.
    fencing_token increments on every acquisition by a NEW owner; a renewal
    by the CURRENT owner leaves it unchanged, since nothing about ownership
    actually changed.
    """
    now = datetime.now(UTC)
    lease = await session.get(TenantLeaseORM, tenant_id, with_for_update=True)

    if lease is None:
        # No row to lock yet -- SELECT ... FOR UPDATE on a nonexistent row
        # locks nothing, so two workers can both reach here for the same
        # brand-new tenant at once. Whichever's INSERT commits first wins;
        # the other's PK violation means "someone else just created it,"
        # not a real error -- roll back this (isolated, single-purpose)
        # transaction and report "not acquired" like any other lost race.
        # A caller that wants this tenant's lease will simply try again on
        # its next renewal tick, which now finds a real row to serialize
        # against via the branch below.
        lease = TenantLeaseORM(
            tenant_id=tenant_id,
            worker_id=worker_id,
            fencing_token=1,
            lease_expires_at=now + timedelta(seconds=ttl_seconds),
            heartbeat_at=now,
        )
        session.add(lease)
        try:
            await session.flush()
        except IntegrityError:
            await session.rollback()
            lease_acquisition_failures_total.inc()
            return None
        return lease.fencing_token

    is_current_owner = lease.worker_id == worker_id
    is_available = lease.worker_id is None or _aware(lease.lease_expires_at) < now
    if not (is_current_owner or is_available):
        lease_acquisition_failures_total.inc()
        return None

    if not is_current_owner:
        lease.fencing_token += 1
    lease.worker_id = worker_id
    lease.lease_expires_at = now + timedelta(seconds=ttl_seconds)
    lease.heartbeat_at = now
    await session.flush()
    return lease.fencing_token


async def verify_lease(session: AsyncSession, tenant_id: int, fencing_token: int) -> bool:
    """The actual safety check: called inside the SAME transaction as the
    risk-state/order write in RiskEngine.approve_and_execute(), never
    standalone. A mismatch means either another worker has since taken over
    this tenant (fencing_token moved on) or this worker's lease lapsed --
    either way, the caller must skip the cycle rather than mint a token or
    place an order. The row lock this takes serializes concurrent
    approve_and_execute() calls for the same tenant globally, so even a
    momentary two-workers-believe-they-own-it window can't produce two
    committed orders.
    """
    now = datetime.now(UTC)
    lease = await session.get(TenantLeaseORM, tenant_id, with_for_update=True)
    if lease is None:
        return False
    return lease.fencing_token == fencing_token and _aware(lease.lease_expires_at) >= now


async def release_lease(session: AsyncSession, tenant_id: int, worker_id: str) -> None:
    """Called on graceful shutdown so another worker can take over
    immediately rather than waiting out the TTL. Only releases a lease this
    worker actually still holds -- a lease already taken over by someone
    else (this worker's renewal lapsed) is never this worker's to touch."""
    lease = await session.get(TenantLeaseORM, tenant_id, with_for_update=True)
    if lease is not None and lease.worker_id == worker_id:
        lease.worker_id = None
        lease.heartbeat_at = datetime.now(UTC)
        await session.flush()
