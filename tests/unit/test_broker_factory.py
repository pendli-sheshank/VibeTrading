from __future__ import annotations

from importlib.util import find_spec

import pytest

from vibetrading.broker.dhan_client import DhanBrokerClient
from vibetrading.broker.factory import get_broker_client
from vibetrading.broker.mock_client import MockBrokerClient
from vibetrading.config import Settings
from vibetrading.core.exceptions import BrokerError


def test_factory_returns_mock_broker_when_no_dhan_credentials():
    settings = Settings(_env_file=None, dhan_client_id="", dhan_access_token="")
    broker = get_broker_client(settings)
    assert isinstance(broker, MockBrokerClient)


def test_factory_attempts_dhan_client_when_credentials_present():
    """Configured Dhan credentials must never silently fall back to paper
    trading. The assertion depends on whether the optional 'dhan' extra is
    installed: without it, construction fails loudly with a message naming
    the missing package; with it, a real DhanBrokerClient is built."""
    settings = Settings(_env_file=None, dhan_client_id="abc", dhan_access_token="xyz")

    if find_spec("dhanhq") is None:
        with pytest.raises(BrokerError, match="dhanhq"):
            get_broker_client(settings)
    else:
        assert isinstance(get_broker_client(settings), DhanBrokerClient)
