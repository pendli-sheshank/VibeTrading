from __future__ import annotations

import pytest

from vibetrading.broker.factory import get_broker_client
from vibetrading.broker.mock_client import MockBrokerClient
from vibetrading.config import Settings
from vibetrading.core.exceptions import BrokerError


def test_factory_returns_mock_broker_when_no_dhan_credentials():
    settings = Settings(_env_file=None, dhan_client_id="", dhan_access_token="")
    broker = get_broker_client(settings)
    assert isinstance(broker, MockBrokerClient)


def test_factory_attempts_dhan_client_when_credentials_present():
    settings = Settings(_env_file=None, dhan_client_id="abc", dhan_access_token="xyz")
    # The 'dhanhq' package isn't installed in this environment (it's an
    # optional extra), so DhanBrokerClient must fail loudly and clearly
    # rather than silently falling back — never fall back to paper trading
    # unannounced when the user explicitly configured live credentials.
    with pytest.raises(BrokerError, match="dhanhq"):
        get_broker_client(settings)
