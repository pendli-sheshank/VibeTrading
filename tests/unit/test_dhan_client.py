"""Regression tests for how DhanBrokerClient reads the dhanhq SDK's replies.

The SDK does not raise on API errors -- it returns
{"status": "failure", "remarks": ..., "data": ""}. Reaching into that
`data` as if it were always a dict is what produced
`AttributeError: 'str' object has no attribute 'get'`, which surfaced as a
500 from the Backtest tab and as an unexplained 0%-confidence Analyze card.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from tenacity import wait_none

from vibetrading.broker.dhan_client import DhanBrokerClient
from vibetrading.core import reliability
from vibetrading.core.exceptions import BrokerError, MarketDataUnavailableError
from vibetrading.core.models import Stock
from vibetrading.core.reliability import get_circuit_breaker, reset_all_circuit_breakers

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


async def test_api_failure_envelope_becomes_a_broker_error_naming_dhans_reason():
    """The exact crash this suite exists for: `data` is an empty STRING on
    failure, so the old parser raised AttributeError instead of reporting
    Dhan's own error code."""
    client = make_client(
        historical_daily_data={"status": "failure", "remarks": "DH-905 : Invalid security id", "data": ""}
    )

    with pytest.raises(BrokerError, match="DH-905"):
        await client.get_historical_candles(STOCK, "1d", NOW - timedelta(days=120), NOW)


async def test_empty_candle_payload_is_reported_as_unavailable_not_as_silence():
    client = make_client(
        historical_daily_data={
            "status": "success",
            "data": {"open": [], "high": [], "low": [], "close": [], "volume": [], "timestamp": []},
        }
    )

    with pytest.raises(MarketDataUnavailableError, match="no candles"):
        await client.get_historical_candles(STOCK, "1d", NOW - timedelta(days=120), NOW)


async def test_candles_parse_into_models():
    client = make_client(
        historical_daily_data={
            "status": "success",
            "data": {
                "open": [100.0, 102.0],
                "high": [105.0, 106.0],
                "low": [99.0, 101.0],
                "close": [104.0, 103.0],
                "volume": [1000, 2000],
                "timestamp": [1_700_000_000, 1_700_086_400],
            },
        }
    )

    candles = await client.get_historical_candles(STOCK, "1d", NOW - timedelta(days=5), NOW)

    assert len(candles) == 2
    assert candles[0].close == 104.0
    assert candles[1].volume == 2000
    assert candles[0].timestamp.tzinfo is not None


async def test_missing_security_id_is_permanent_and_never_trips_the_circuit_breaker():
    """A stock with no security ID is a configuration gap, not a sign that
    Dhan is unhealthy. Counting it would let one unconfigured stock fail
    every other call on the same account."""
    client = make_client(client_id="acct-1")
    unmapped = Stock(symbol="NOSECID")

    for _ in range(10):
        with pytest.raises(MarketDataUnavailableError, match="security ID"):
            await client.get_historical_candles(unmapped, "1d", NOW - timedelta(days=30), NOW)

    assert get_circuit_breaker("dhan:acct-1").state == "closed"


async def test_repeated_real_api_failures_do_trip_the_circuit_breaker():
    client = make_client(
        client_id="acct-2",
        historical_daily_data={"status": "failure", "remarks": "DH-901 : Invalid token", "data": ""},
    )

    for _ in range(5):
        with pytest.raises(BrokerError):
            await client.get_historical_candles(STOCK, "1d", NOW - timedelta(days=30), NOW)

    assert get_circuit_breaker("dhan:acct-2").state == "open"


async def test_quote_parses_price_and_ohlc():
    client = make_client(
        quote_data={
            "status": "success",
            "data": {
                "NSE_EQ": {
                    "2885": {
                        "last_price": 1250.5,
                        "volume": 4_500_000,
                        "ohlc": {"open": 1240.0, "high": 1260.0, "low": 1235.0, "close": 1245.0},
                    }
                }
            },
        }
    )

    quote = await client.get_quote(STOCK)

    assert quote.status.value == "live"
    assert quote.last_price == 1250.5
    assert quote.previous_close == 1245.0
    assert quote.change == pytest.approx(5.5)
    assert quote.change_pct == pytest.approx(0.44, abs=0.01)
    assert quote.volume == 4_500_000


async def test_quote_failure_envelope_raises_rather_than_returning_zeros():
    client = make_client(quote_data={"status": "failure", "remarks": "DH-906 : Rate limit", "data": ""})

    with pytest.raises(BrokerError, match="DH-906"):
        await client.get_quote(STOCK)


async def test_option_chain_computes_pcr_and_atm_from_real_rows():
    client = make_client(
        expiry_list={"status": "success", "data": ["2026-09-25"]},
        option_chain={
            "status": "success",
            "data": {
                "last_price": 1250.0,
                "oc": {
                    "1200.000000": {
                        "ce": {"oi": 1000, "previous_oi": 900, "volume": 50, "implied_volatility": 18.5, "last_price": 55.0},
                        "pe": {"oi": 400, "previous_oi": 400, "volume": 20, "implied_volatility": 17.0, "last_price": 5.0},
                    },
                    "1250.000000": {
                        "ce": {"oi": 2000, "previous_oi": 1500, "volume": 80, "implied_volatility": 16.0, "last_price": 20.0},
                        "pe": {"oi": 3000, "previous_oi": 2000, "volume": 90, "implied_volatility": 16.5, "last_price": 22.0},
                    },
                },
            },
        },
    )

    chain = await client.get_option_chain(STOCK)

    assert chain.status.value == "live"
    assert chain.expiry == "2026-09-25"
    assert chain.atm_strike == 1250.0
    assert chain.total_call_oi == 3000
    assert chain.total_put_oi == 3400
    assert chain.put_call_ratio == pytest.approx(3400 / 3000, abs=0.001)

    atm = next(s for s in chain.strikes if s.strike == 1250.0)
    assert atm.call_oi_change == 500
    assert atm.put_iv == 16.5
    # A genuinely flat OI must read as 0, not as "unknown".
    flat = next(s for s in chain.strikes if s.strike == 1200.0)
    assert flat.put_oi_change == 0


async def test_option_chain_without_listed_expiries_is_unavailable_not_fabricated():
    client = make_client(expiry_list={"status": "success", "data": []})

    with pytest.raises(MarketDataUnavailableError, match="expiries"):
        await client.get_option_chain(STOCK)


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


async def test_an_error_object_with_no_details_produces_a_readable_message():
    """Dhan sometimes returns an error object whose fields are all null.
    Dumping that dict at the user explains nothing."""
    client = make_client(
        quote_data={
            "status": "failure",
            "remarks": {"error_code": None, "error_type": None, "error_message": None},
            "data": "",
        }
    )

    with pytest.raises(BrokerError) as exc_info:
        await client.get_quote(STOCK)

    message = str(exc_info.value)
    assert "error_code" not in message  # no raw dict
    assert "access token" in message


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
