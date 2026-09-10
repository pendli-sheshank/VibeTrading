from __future__ import annotations

import asyncio
import logging

from vibetrading.broker.base import BrokerClient
from vibetrading.broker.factory import get_broker_client
from vibetrading.config import Settings
from vibetrading.orchestrator.scheduler import OrchestratorScheduler, build_scheduler
from vibetrading.persistence.db import get_session
from vibetrading.settings.cache import get_tenant_settings
from vibetrading.settings.registry import fields_in_section
from vibetrading.settings.service import load_settings_from_db

logger = logging.getLogger(__name__)


class OrchestratorRuntime:
    """Owns one tenant's current broker + OrchestratorScheduler and can
    rebuild both on demand — e.g. after a settings change to execution
    mode, Dhan credentials, watchlist, scheduling intervals, or LLM
    provider, all of which are "baked in" at construction time in ways
    that can't just mutate cleanly (the broker is a concrete class chosen
    once, APScheduler jobs fix their interval at registration, agents
    capture a concrete LLMAdapter once).

    One instance per tenant, held by orchestrator.manager.MultiTenantRuntimeManager
    (see that module) rather than a module-level singleton, so tests can
    build isolated runtimes without process-global state leaking between
    them.
    """

    def __init__(self, tenant_id: int, settings: Settings | None = None):
        self.tenant_id = tenant_id
        self._settings = settings or get_tenant_settings(tenant_id)
        self._broker: BrokerClient | None = None
        self._scheduler: OrchestratorScheduler | None = None
        self._lock = asyncio.Lock()

    @property
    def broker(self) -> BrokerClient:
        if self._broker is None:
            raise RuntimeError("OrchestratorRuntime.start() has not been called yet.")
        return self._broker

    @property
    def scheduler(self) -> OrchestratorScheduler | None:
        return self._scheduler

    async def start(self) -> None:
        async with self._lock:
            # Only start() reloads from DB -- this tenant's very first
            # build in this process needs it (get_tenant_settings() just
            # constructs class defaults, it doesn't know about anything
            # saved earlier). restart() deliberately does NOT reload here:
            # every real restart() call is triggered right after
            # save_settings() already refreshed self._settings from DB, so
            # reloading again would be redundant at best -- and at worst
            # would silently revert an in-memory-only settings mutation
            # that was never meant to be persisted (this is also what
            # keeps tests that poke `settings.some_field = ...` directly,
            # without a full save_settings() round-trip, behaving as
            # written).
            async with get_session() as session:
                await load_settings_from_db(session, self.tenant_id, self._settings)
            self._broker, self._scheduler = await self._build()
            if self._scheduler is not None:
                self._scheduler.start()
            logger.info(
                "OrchestratorRuntime started (broker=%s, scheduler=%s).",
                type(self._broker).__name__,
                "enabled" if self._scheduler else "disabled",
            )

    async def restart(self) -> None:
        """Rebuilds the broker+scheduler from current settings — and,
        deliberately, builds the NEW pair fully before touching the OLD one.
        A settings change that makes the new broker impossible to construct
        (a typo'd Dhan credential, the dhanhq extra not being installed,
        etc.) raises here with the previous broker+scheduler left running
        untouched, rather than tearing them down first and leaving the
        whole app without any broker at all until another settings change
        happens to succeed.

        Only once the new pair builds successfully does the old scheduler
        get drained — waiting for any in-flight job to finish naturally,
        see OrchestratorScheduler.shutdown_gracefully() — and swapped out.
        Never discards an order placement mid-flight.
        """
        async with self._lock:
            new_broker, new_scheduler = await self._build()

            old_scheduler = self._scheduler
            if old_scheduler is not None:
                await old_scheduler.shutdown_gracefully()

            self._broker, self._scheduler = new_broker, new_scheduler
            if self._scheduler is not None:
                self._scheduler.start()
            logger.info(
                "OrchestratorRuntime restarted (broker=%s, scheduler=%s).",
                type(self._broker).__name__,
                "enabled" if self._scheduler else "disabled",
            )

    async def shutdown(self) -> None:
        async with self._lock:
            await self._shutdown_locked()

    async def _build(self) -> tuple[BrokerClient, OrchestratorScheduler | None]:
        broker = get_broker_client(self._settings)
        scheduler = None
        if self._settings.enable_scheduler:
            scheduler = await build_scheduler(broker, self.tenant_id, self._settings)
        return broker, scheduler

    async def _shutdown_locked(self) -> None:
        if self._scheduler is not None:
            await self._scheduler.shutdown_gracefully()
        self._scheduler = None
        self._broker = None


def should_restart(changed_keys: set[str]) -> bool:
    """Decides whether a settings save needs an OrchestratorRuntime.restart().

    True for any non-empty change, EXCEPT when every changed key belongs to
    the risk_limits section — those already take effect on the very next
    RiskEngine call via the mutable-settings-singleton mechanism (see
    settings/service.py), so restarting for them would only rebuild the
    broker/scheduler/LLM adapters for no behavioral benefit. Deliberately
    no finer-grained per-field dirty-checking beyond this one section-level,
    provably-safe exemption — see test_settings_registry.py, which pins
    this section's key-set to exactly what RiskConfig.from_settings() reads
    so the exemption can never silently drift out of sync.
    """
    if not changed_keys:
        return False
    risk_limit_keys = {f.key for f in fields_in_section("risk_limits")}
    return not changed_keys.issubset(risk_limit_keys)
