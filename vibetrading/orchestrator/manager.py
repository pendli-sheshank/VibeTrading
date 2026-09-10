from __future__ import annotations

import asyncio
import logging

from sqlalchemy import select

from vibetrading.orchestrator.runtime import OrchestratorRuntime
from vibetrading.persistence.db import get_session
from vibetrading.persistence.orm_models import UserORM

logger = logging.getLogger(__name__)


class MultiTenantRuntimeManager:
    """Owns one OrchestratorRuntime per tenant -- what app.state.runtime
    used to be as a single process-wide instance is now
    app.state.runtime_manager, a dict[tenant_id, OrchestratorRuntime]
    behind this class. Each tenant's broker/scheduler is fully independent;
    nothing here is shared across tenants.

    This is still a single-PROCESS manager -- every tenant's scheduler runs
    in this one process. The distributed, multi-worker-safe version (many
    processes, exactly one active owner per tenant via a durable lease) is
    Phase 20's job; this class is the seam that phase rebuilds around.
    """

    def __init__(self):
        self._runtimes: dict[int, OrchestratorRuntime] = {}
        self._lock = asyncio.Lock()

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
        start_all_existing_tenants() sweep already ran."""
        async with self._lock:
            runtime = self._runtimes.get(tenant_id)
            if runtime is None:
                runtime = OrchestratorRuntime(tenant_id=tenant_id)
                await runtime.start()
                self._runtimes[tenant_id] = runtime
                logger.info("Started OrchestratorRuntime for tenant_id=%s", tenant_id)
            return runtime

    def get(self, tenant_id: int) -> OrchestratorRuntime | None:
        return self._runtimes.get(tenant_id)

    async def shutdown_all(self) -> None:
        async with self._lock:
            for runtime in self._runtimes.values():
                await runtime.shutdown()
            self._runtimes.clear()
