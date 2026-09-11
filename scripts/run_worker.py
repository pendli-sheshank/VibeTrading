#!/usr/bin/env python3
"""Background Worker entrypoint (Render's worker service type, see
render.yaml) -- owns every tenant's autonomous trading loop, competing
for and renewing each tenant's lease (see orchestrator/lease.py),
without serving any HTTP traffic at all. The dashboard/API side of the
split (api/app.py, run with uvicorn) is a separate deployable that only
ever reads a tenant's broker state, never runs a scheduler -- see
config.py's worker_role and orchestrator/manager.py's manage_leases.

Usage:
    WORKER_ROLE=worker python scripts/run_worker.py

WORKER_ROLE=worker isn't required for this script to behave correctly --
MultiTenantRuntimeManager(manage_leases=True) here always owns leases
regardless -- but set it anyway so structured logs and any future
role-conditional behavior stay consistent with the Web Service side.
"""

from __future__ import annotations

import asyncio
import logging
import signal

from vibetrading.logging_conf import configure_logging
from vibetrading.orchestrator.manager import MultiTenantRuntimeManager
from vibetrading.persistence.db import init_db

logger = logging.getLogger(__name__)


async def main() -> None:
    configure_logging()
    await init_db()

    manager = MultiTenantRuntimeManager(manage_leases=True)
    await manager.start_all_existing_tenants()
    manager.start_lease_loop()
    logger.info("Background worker started (worker_id=%s).", manager.worker_id)

    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, stop.set)

    await stop.wait()
    logger.info("Shutdown signal received; draining every tenant's scheduler and releasing leases.")
    await manager.shutdown_all()
    logger.info("Background worker stopped cleanly.")


if __name__ == "__main__":
    asyncio.run(main())
