"""How DhanBrokerClient reads the dhanhq SDK's replies, on the trading path.

The SDK does not raise on API errors -- it returns
{"status": "failure", "remarks": ..., "data": ""}, where `data` is an empty
STRING. Reading it as a dict used to raise
`AttributeError: 'str' object has no attribute 'get'` instead of reporting
Dhan's own reason, so every envelope is checked before it is parsed.

Market data has since moved to vibetrading/marketdata/providers/ (covered by
test_market_data_providers.py); Dhan is only asked to trade.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from tenacity import wait_none

from vibetrading.broker.dhan_client import DhanBrokerClient
from vibetrading.core import reliability
from vibetrading.core.exceptions import BrokerError
from vibetrading.core.models import Stock
from vibetrading.core.reliability import reset_all_circuit_breakers

STOCK = Stock(symbol="RELIANCE", dhan_security_id="2885")
NOW = datetime.now(UTC)


class FakeSDK:
    """Mimics dhanhq's real return shapes (dicts, never exceptions)."""

    NSE = "NSE_EQ"

    def __init__(self, **responses):
        self.responses = responses
        self.calls: list[str] = []

    def _reply(self, name):
        self.calls.append(name)
        value = self.responses.get(name, {"status": "success", "data": {}})
        if isinstance(value, Exception):
            raise value
        return value

    def historical_daily_data(self, **kwargs):
        return self._reply("historical_daily_data")

    def quote_data(self, securities):
        return self._reply("quote_data")

    def expiry_list(self, **kwargs):
        return self._reply("expiry_list")

    def option_chain(self, **kwargs):
        return self._reply("option_chain")

    def get_fund_limits(self):
        return self._reply("get_fund_limits")

    def get_positions(self):
        return self._reply("get_positions")


def make_client(client_id: str = "test-client", **responses) -> DhanBrokerClient:
    client = DhanBrokerClient.__new__(DhanBrokerClient)
    client._client = FakeSDK(**responses)
    client._client_id = client_id
    client._security_id_cache = {}
    return client


@pytest.fixture(autouse=True)
def _clean_breakers():
    reset_all_circuit_breakers()
    yield
    reset_all_circuit_breakers()


@pytest.fixture(autouse=True)
def _no_retry_backoff(monkeypatch):
    """Keep the retry/breaker behavior under test, drop only the sleeping.
    Without this, the 5-failures-to-open test spends ~a minute in
    exponential backoff waits."""
    monkeypatch.setattr(reliability, "wait_exponential_jitter", lambda **kwargs: wait_none())


async def test_placing_an_order_without_a_security_id_says_orders_need_one():
    """Security IDs are required for ORDERS only -- analysis never asks Dhan
    for anything, so the message must not imply the stock can't be studied."""
    from vibetrading.core.enums import ExecutionMode, OrderSide
    from vibetrading.core.models import OrderRequest
    from vibetrading.risk.tokens import mint_token

    client = make_client()
    request = OrderRequest(
        stock_symbol="NOSECID", side=OrderSide.BUY, quantity=1, mode=ExecutionMode.PAPER
    )

    token = mint_token(signal_id=None, stock_symbol="NOSECID", quantity=1)

    with pytest.raises(BrokerError, match="Cannot place an order"):
        await client.place_order(request, token)


async def test_fund_limits_failure_envelope_raises():
    client = make_client(get_fund_limits={"status": "failure", "remarks": "DH-901 : Invalid token", "data": ""})

    with pytest.raises(BrokerError, match="DH-901"):
        await client.get_funds()


async def test_positions_failure_envelope_raises_instead_of_reporting_no_positions():
    """Reporting "no open positions" when the call actually failed would
    understate real exposure -- the one direction this must never err in."""
    client = make_client(get_positions={"status": "failure", "remarks": "DH-901 : Invalid token", "data": ""})

    with pytest.raises(BrokerError, match="DH-901"):
        await client.get_positions()


async def test_a_structured_error_object_keeps_its_code_and_message():
    client = make_client(
        get_fund_limits={
            "status": "failure",
            "remarks": {
                "error_code": "DH-901",
                "error_type": "Invalid_Authentication",
                "error_message": "Client ID or user generated access token is invalid or expired.",
            },
            "data": "",
        }
    )

    with pytest.raises(BrokerError, match="DH-901 : Client ID or user generated access token"):
        await client.get_funds()
