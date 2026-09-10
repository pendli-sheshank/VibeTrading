from __future__ import annotations

import asyncio
import logging

from vibetrading.broker.base import BrokerClient
from vibetrading.broker.factory import get_broker_client
from vibetrading.config import Settings, get_settings
from vibetrading.orchestrator.scheduler import OrchestratorScheduler, build_scheduler
from vibetrading.settings.registry import fields_in_section

logger = logging.getLogger(__name__)


class OrchestratorRuntime:
    """Owns the process's current broker + OrchestratorScheduler and can
    rebuild both on demand — e.g. after a settings change to execution
    mode, Dhan credentials, watchlist, scheduling intervals, or LLM
    provider, all of which are "baked in" at construction time in ways
    that can't just mutate cleanly (the broker is a concrete class chosen
    once, APScheduler jobs fix their interval at registration, agents
    capture a concrete LLMAdapter once).

    Stashed on app.state.runtime (not a module-level singleton), so tests
    can build isolated runtimes without process-global state leaking
    between them.
    """

    def __init__(self, settings: Settings | None = None):
        self._settings = settings or get_settings()
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
            await self._start_locked()

    async def restart(self) -> None:
        """Tears down the current broker+scheduler — waiting for any
        in-flight job to finish naturally first, see
        OrchestratorScheduler.shutdown_gracefully() — and rebuilds both
        from current settings. Never discards an order placement
        mid-flight."""
        async with self._lock:
            await self._shutdown_locked()
            await self._start_locked()

    async def shutdown(self) -> None:
        async with self._lock:
            await self._shutdown_locked()

    async def _start_locked(self) -> None:
        self._broker = get_broker_client(self._settings)
        if self._settings.enable_scheduler:
            self._scheduler = await build_scheduler(self._broker, self._settings)
            self._scheduler.start()
        else:
            self._scheduler = None
        logger.info(
            "OrchestratorRuntime started (broker=%s, scheduler=%s).",
            type(self._broker).__name__,
            "enabled" if self._scheduler else "disabled",
        )

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
