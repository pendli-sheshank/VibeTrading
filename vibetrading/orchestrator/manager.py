from __future__ import annotations

import asyncio
import logging
import os
import socket
import uuid

from sqlalchemy import select

from vibetrading.orchestrator.lease import (
    DEFAULT_LEASE_TTL_SECONDS,
    DEFAULT_RENEWAL_INTERVAL_SECONDS,
    acquire_or_renew_lease,
    release_lease,
)
from vibetrading.orchestrator.runtime import OrchestratorRuntime
from vibetrading.persistence.db import get_session
from vibetrading.persistence.orm_models import UserORM

logger = logging.getLogger(__name__)


def _generate_worker_id() -> str:
    return f"{socket.gethostname()}-{os.getpid()}-{uuid.uuid4().hex[:8]}"


class MultiTenantRuntimeManager:
    """Owns one OrchestratorRuntime per tenant -- what app.state.runtime
    used to be as a single process-wide instance is now
    app.state.runtime_manager, a dict[tenant_id, OrchestratorRuntime]
    behind this class. Each tenant's broker/scheduler is fully independent;
    nothing here is shared across tenants.

    Also runs a background lease-renewal loop (see orchestrator/lease.py):
    every renewal_interval_seconds, this worker attempts to
    acquire-or-renew the lease for every tenant it's tracking. A tenant
    whose lease this worker holds gets its scheduler running (fenced with
    the acquired token, enforced again inside every
    RiskEngine.approve_and_execute() call -- see risk/engine.py); a tenant
    whose lease it does NOT hold gets its scheduler stopped locally, so a
    worker fleet with N replicas never runs N redundant copies of the same
    tenant's autonomous trading loop. The runtime object itself (broker,
    dashboard access) is unaffected either way -- losing a lease never
    tears down a tenant's dashboard-facing broker, only its unattended
    scheduler.

    Correctness never depends on this loop running promptly, or at all --
    it's a resource-usage optimization, not a safety mechanism. The actual
    safety guarantee is the fencing check inside approve_and_execute()'s
    own transaction, which rejects a stale worker's order attempt even if
    this loop somehow never got around to stopping its scheduler.
    """

    def __init__(
        self,
        renewal_interval_seconds: int = DEFAULT_RENEWAL_INTERVAL_SECONDS,
        lease_ttl_seconds: int = DEFAULT_LEASE_TTL_SECONDS,
        worker_id: str | None = None,
    ):
        self._runtimes: dict[int, OrchestratorRuntime] = {}
        self._lock = asyncio.Lock()
        self.worker_id = worker_id or _generate_worker_id()
        self._renewal_interval_seconds = renewal_interval_seconds
        self._lease_ttl_seconds = lease_ttl_seconds
        self._lease_loop_task: asyncio.Task | None = None

    async def start_all_existing_tenants(self) -> None:
        """Called once from the app's lifespan: boots a runtime for every
        already-registered user, so trading resumes across a process
        restart without anyone needing to touch the dashboard first."""
        async with get_session() as session:
            result = await session.execute(select(UserORM.id))
            tenant_ids = [row[0] for row in result.all()]
        for tenant_id in tenant_ids:
            await self.get_or_start(tenant_id)

    async def get_or_start(self, tenant_id: int) -> OrchestratorRuntime:
        """Returns the tenant's runtime, starting one on first access --
        covers a user who registered after the lifespan's initial
        start_all_existing_tenants() sweep already ran. Always attempts a
        real lease acquisition before the very first start() -- so a
        freshly-created runtime's scheduler is correctly fenced (or held
        off entirely, if another worker already owns this tenant) from its
        first build, never running briefly unfenced while waiting for the
        next periodic renewal tick."""
        async with self._lock:
            runtime = self._runtimes.get(tenant_id)
            if runtime is None:
                runtime = OrchestratorRuntime(tenant_id=tenant_id)
                held, fencing_token = await self._try_acquire(tenant_id)
                await runtime.set_lease(held, fencing_token)
                await runtime.start()
                self._runtimes[tenant_id] = runtime
                logger.info(
                    "Started OrchestratorRuntime for tenant_id=%s (lease_held=%s)", tenant_id, held
                )
            return runtime

    def get(self, tenant_id: int) -> OrchestratorRuntime | None:
        return self._runtimes.get(tenant_id)

    def start_lease_loop(self) -> None:
        """Starts the background acquire/renew loop as an asyncio task.
        Call once from the app's lifespan, after start_all_existing_tenants().
        A no-op if already running."""
        if self._lease_loop_task is None or self._lease_loop_task.done():
            self._lease_loop_task = asyncio.create_task(self._lease_loop())

    async def _lease_loop(self) -> None:
        while True:
            try:
                await asyncio.sleep(self._renewal_interval_seconds)
                await self._renew_all_leases()
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("Lease renewal loop iteration failed; will retry next interval.")

    async def _renew_all_leases(self) -> None:
        tenant_ids = list(self._runtimes.keys())
        for tenant_id in tenant_ids:
            await self._renew_one_lease(tenant_id)

    async def _renew_one_lease(self, tenant_id: int) -> None:
        runtime = self._runtimes.get(tenant_id)
        if runtime is None:
            return
        held, fencing_token = await self._try_acquire(tenant_id)
        if not held:
            logger.info("Lease for tenant_id=%s is held by another worker; scheduler stays stopped here.", tenant_id)
        await runtime.set_lease(held, fencing_token)

    async def _try_acquire(self, tenant_id: int) -> tuple[bool, int | None]:
        async with get_session() as session:
            fencing_token = await acquire_or_renew_lease(session, tenant_id, self.worker_id, self._lease_ttl_seconds)
            await session.commit()
        return fencing_token is not None, fencing_token

    async def shutdown_all(self) -> None:
        if self._lease_loop_task is not None:
            self._lease_loop_task.cancel()
            try:
                await self._lease_loop_task
            except asyncio.CancelledError:
                pass
            self._lease_loop_task = None

        async with self._lock:
            for tenant_id, runtime in self._runtimes.items():
                await runtime.shutdown()
                async with get_session() as session:
                    await release_lease(session, tenant_id, self.worker_id)
                    await session.commit()
            self._runtimes.clear()
