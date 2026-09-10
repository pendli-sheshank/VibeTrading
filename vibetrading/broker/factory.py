from __future__ import annotations

import logging

from vibetrading.broker.base import BrokerClient
from vibetrading.broker.mock_client import MockBrokerClient
from vibetrading.config import Settings, get_settings

logger = logging.getLogger(__name__)


def get_broker_client(settings: Settings | None = None) -> BrokerClient:
    """Selects the Execution Agent implementation: DhanBrokerClient when
    credentials are configured, otherwise MockBrokerClient — loudly logged
    either way, and always visible on the dashboard's mode banner.
    """
    settings = settings or get_settings()

    if not settings.has_dhan_credentials:
        logger.warning("No Dhan credentials configured; using MockBrokerClient (paper trading only).")
        return MockBrokerClient()

    from vibetrading.broker.dhan_client import DhanBrokerClient  # lazy: optional dependency

    logger.info("Dhan credentials configured; using DhanBrokerClient.")
    return DhanBrokerClient(client_id=settings.dhan_client_id, access_token=settings.dhan_access_token)
