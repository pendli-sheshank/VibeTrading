"""The free, key-less market-data providers that replaced broker market data.

The point of this layer: analysis is addressed by ticker, so no broker
security ID is needed to look at a stock. These tests pin that mapping, the
parsing of each provider's real response shape, and the honest failure
behavior when a provider is unreachable or blocked.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import httpx
import pytest
from tenacity import wait_none

from vibetrading.config import Settings
from vibetrading.core import reliability
from vibetrading.core.exceptions import MarketDataError, MarketDataUnavailableError
from vibetrading.core.models import Stock
from vibetrading.core.reliability import reset_all_circuit_breakers
from vibetrading.marketdata.providers import (
    LiveMarketDataProvider,
    NseOptionChainSource,
    SimulatedMarketDataProvider,
    YahooMarketDataProvider,
    get_market_data_provider,
    yahoo_symbol,
)

NOW = datetime.now(UTC)


@pytest.fixture(autouse=True)
def _clean_breakers():
    reset_all_circuit_breakers()
    yield
    reset_all_circuit_breakers()


@pytest.fixture(autouse=True)
def _no_retry_backoff(monkeypatch):
    monkeypatch.setattr(reliability, "wait_exponential_jitter", lambda **kwargs: wait_none())


def yahoo_client(handler) -> YahooMarketDataProvider:
    return YahooMarketDataProvider(client=httpx.AsyncClient(transport=httpx.MockTransport(handler)))


def chart_payload(*, closes, timestamps, meta=None) -> dict:
    return {
        "chart": {
            "result": [
                {
                    "meta": meta or {},
                    "timestamp": timestamps,
                    "indicators": {
                        "quote": [
                            {
                                "open": closes,
                                "high": [c + 1 for c in closes],
                                "low": [c - 1 for c in closes],
                                "close": closes,
                                "volume": [1000] * len(closes),
                            }
                        ]
                    },
                }
            ],
            "error": None,
        }
    }


# --------------------------------------------------------------------------
# Symbol mapping -- the reason no security ID is needed
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("stock", "expected"),
    [
        (Stock(symbol="RELIANCE"), "RELIANCE.NS"),
        (Stock(symbol="TCS", exchange="NSE"), "TCS.NS"),
        (Stock(symbol="TCS", exchange="BSE"), "TCS.BO"),
        (Stock(symbol="nifty"), "^NSEI"),
        (Stock(symbol="BANKNIFTY"), "^NSEBANK"),
        (Stock(symbol="SENSEX"), "^BSESN"),
        (Stock(symbol="^NSEI"), "^NSEI"),
    ],
)
def test_tickers_map_without_any_broker_identifier(stock, expected):
    assert yahoo_symbol(stock) == expected


# --------------------------------------------------------------------------
# Yahoo
# --------------------------------------------------------------------------


async def test_quote_reads_the_live_price_and_change():
    def handler(request: httpx.Request) -> httpx.Response:
        assert "RELIANCE.NS" in str(request.url)
        return httpx.Response(
            200,
            json=chart_payload(
                closes=[1240.0],
                timestamps=[int(NOW.timestamp())],
                meta={
                    "regularMarketPrice": 1243.9,
                    "chartPreviousClose": 1257.5,
                    "regularMarketDayHigh": 1253.4,
                    "regularMarketDayLow": 1238.5,
                    "regularMarketOpen": 1250.0,
                    "regularMarketVolume": 7703749,
                    "regularMarketTime": int(NOW.timestamp()),
                },
            ),
        )

    quote = await yahoo_client(handler).get_quote(Stock(symbol="RELIANCE"))

    assert quote.status.value == "live"
    assert quote.last_price == 1243.9
    assert quote.previous_close == 1257.5
    assert quote.change == pytest.approx(-13.6)
    assert quote.change_pct == pytest.approx(-1.08, abs=0.01)
    assert quote.volume == 7703749


async def test_historical_candles_request_an_exact_window_not_a_range_token():
    """A `range` token is anchored to today, so a backtest over a past period
    would quietly get the most recent candles instead. Exact epoch bounds are
    what make a historical window mean what it says."""
    seen: dict[str, str] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen.update(dict(request.url.params))
        return httpx.Response(
            200,
            json=chart_payload(
                closes=[100.0, 101.0, 102.0],
                timestamps=[int((NOW - timedelta(days=d)).timestamp()) for d in (3, 2, 1)],
            ),
        )

    from_date, to_date = NOW - timedelta(days=30), NOW
    candles = await yahoo_client(handler).get_historical_candles(
        Stock(symbol="TCS"), "1d", from_date, to_date
    )

    assert seen["period1"] == str(int(from_date.timestamp()))
    assert seen["period2"] == str(int(to_date.timestamp()))
    assert "range" not in seen
    assert [c.close for c in candles] == [100.0, 101.0, 102.0]
    assert all(c.timestamp.tzinfo is not None for c in candles)


async def test_null_padded_sessions_are_skipped_not_invented():
    """Yahoo pads holidays with nulls. Filling them would fabricate bars that
    never traded."""

    def handler(request: httpx.Request) -> httpx.Response:
        payload = chart_payload(
            closes=[100.0, 101.0],
            timestamps=[int((NOW - timedelta(days=d)).timestamp()) for d in (2, 1)],
        )
        series = payload["chart"]["result"][0]["indicators"]["quote"][0]
        series["close"] = [100.0, None]
        return httpx.Response(200, json=payload)

    candles = await yahoo_client(handler).get_historical_candles(
        Stock(symbol="TCS"), "1d", NOW - timedelta(days=5), NOW
    )

    assert len(candles) == 1
    assert candles[0].close == 100.0


async def test_an_unknown_ticker_is_permanent_and_does_not_trip_the_breaker():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(404, json={"chart": {"result": None, "error": {"code": "Not Found"}}})

    provider = yahoo_client(handler)
    for _ in range(10):
        with pytest.raises(MarketDataUnavailableError, match="no instrument called"):
            await provider.get_quote(Stock(symbol="NOSUCHTICKER"))

    assert reliability.get_circuit_breaker("marketdata:yahoo").state == "closed"


async def test_a_server_error_is_retryable_and_eventually_trips_the_breaker():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(503, text="upstream unavailable")

    provider = yahoo_client(handler)
    for _ in range(5):
        with pytest.raises(MarketDataError, match="HTTP 503"):
            await provider.get_quote(Stock(symbol="RELIANCE"))

    assert reliability.get_circuit_breaker("marketdata:yahoo").state == "open"


async def test_a_quote_with_no_traded_price_is_unavailable_rather_than_zero():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=chart_payload(closes=[], timestamps=[], meta={}))

    with pytest.raises(MarketDataUnavailableError, match="no traded price"):
        await yahoo_client(handler).get_quote(Stock(symbol="RELIANCE"))


async def test_yahoo_has_no_option_chain_and_says_so():
    with pytest.raises(MarketDataUnavailableError, match="does not provide option-chain"):
        await YahooMarketDataProvider().get_option_chain(Stock(symbol="RELIANCE"))


# --------------------------------------------------------------------------
# NSE option chain
# --------------------------------------------------------------------------


def nse_source(handler) -> NseOptionChainSource:
    return NseOptionChainSource(client=httpx.AsyncClient(transport=httpx.MockTransport(handler)))


def option_row(strike: float, *, ce_oi: int, pe_oi: int, expiry: str = "25-Sep-2026") -> dict:
    return {
        "strikePrice": strike,
        "expiryDate": expiry,
        "CE": {
            "openInterest": ce_oi,
            "changeinOpenInterest": 100,
            "totalTradedVolume": 50,
            "impliedVolatility": 18.5,
            "lastPrice": 55.0,
        },
        "PE": {
            "openInterest": pe_oi,
            "changeinOpenInterest": -40,
            "totalTradedVolume": 20,
            "impliedVolatility": 17.0,
            "lastPrice": 5.0,
        },
    }


async def test_option_chain_computes_pcr_and_atm_from_the_nearest_expiry():
    def handler(request: httpx.Request) -> httpx.Response:
        if "option-chain" in str(request.url) and "api" not in str(request.url):
            return httpx.Response(200, text="<html>cookie bootstrap</html>")
        return httpx.Response(
            200,
            json={
                "records": {
                    "expiryDates": ["25-Sep-2026", "30-Oct-2026"],
                    "underlyingValue": 1250.0,
                    "data": [
                        option_row(1200.0, ce_oi=1000, pe_oi=400),
                        option_row(1250.0, ce_oi=2000, pe_oi=3000),
                        # A later expiry must not be mixed into the near one.
                        option_row(1250.0, ce_oi=9999, pe_oi=9999, expiry="30-Oct-2026"),
                    ],
                }
            },
        )

    chain = await nse_source(handler).get_option_chain(Stock(symbol="RELIANCE"))

    assert chain.status.value == "live"
    assert chain.expiry == "25-Sep-2026"
    assert chain.atm_strike == 1250.0
    assert chain.total_call_oi == 3000
    assert chain.total_put_oi == 3400
    assert chain.put_call_ratio == pytest.approx(3400 / 3000, abs=0.001)
    assert {s.strike for s in chain.strikes} == {1200.0, 1250.0}

    atm = next(s for s in chain.strikes if s.strike == 1250.0)
    assert atm.call_iv == 18.5
    assert atm.put_oi_change == -40


@pytest.mark.parametrize("status", [401, 403])
async def test_nse_blocking_this_host_is_reported_as_unavailable_not_retried(status):
    """NSE blocks most server IPs. That is a fact to state plainly, not an
    error to retry or a reason to show a fabricated chain."""

    def handler(request: httpx.Request) -> httpx.Response:
        if "api" not in str(request.url):
            return httpx.Response(200, text="<html></html>")
        return httpx.Response(status, text="Access Denied")

    with pytest.raises(MarketDataUnavailableError, match="blocks automated access"):
        await nse_source(handler).get_option_chain(Stock(symbol="RELIANCE"))


async def test_nse_bot_protection_html_is_not_mistaken_for_data():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text="<html>Please enable JavaScript</html>")

    with pytest.raises(MarketDataUnavailableError, match="non-JSON"):
        await nse_source(handler).get_option_chain(Stock(symbol="RELIANCE"))


# --------------------------------------------------------------------------
# Simulated + factory
# --------------------------------------------------------------------------


async def test_simulated_provider_is_deterministic_and_never_labelled_live():
    quote_a = await SimulatedMarketDataProvider(seed=5).get_quote(Stock(symbol="INFY"))
    quote_b = await SimulatedMarketDataProvider(seed=5).get_quote(Stock(symbol="INFY"))

    assert quote_a.status.value == "simulated"
    assert quote_a.last_price == quote_b.last_price


def test_live_is_the_shipped_default_needing_no_configuration():
    """Checked on the field default rather than a constructed Settings: the
    test suite sets MARKET_DATA_PROVIDER=simulated in the environment, which
    a constructed Settings would pick up."""
    assert Settings.model_fields["market_data_provider"].default == "live"


def test_factory_maps_each_setting_to_its_provider():
    assert isinstance(
        get_market_data_provider(Settings(_env_file=None, market_data_provider="live")),
        LiveMarketDataProvider,
    )
    assert isinstance(
        get_market_data_provider(Settings(_env_file=None, market_data_provider="simulated")),
        SimulatedMarketDataProvider,
    )


async def test_live_provider_keeps_prices_working_when_the_option_source_is_blocked():
    """The two sources fail independently: no option chain must never cost
    the quote or the indicators."""

    def handler(request: httpx.Request) -> httpx.Response:
        if "nseindia" in str(request.url):
            return httpx.Response(403, text="Access Denied")
        return httpx.Response(
            200,
            json=chart_payload(
                closes=[100.0],
                timestamps=[int(NOW.timestamp())],
                meta={"regularMarketPrice": 100.0, "regularMarketTime": int(NOW.timestamp())},
            ),
        )

    provider = LiveMarketDataProvider(client=httpx.AsyncClient(transport=httpx.MockTransport(handler)))

    quote = await provider.get_quote(Stock(symbol="RELIANCE"))
    assert quote.last_price == 100.0

    with pytest.raises(MarketDataUnavailableError):
        await provider.get_option_chain(Stock(symbol="RELIANCE"))


async def test_provider_instances_are_reused_so_connection_pools_are_not_leaked():
    """A fresh provider per request would open a new HTTP connection pool on
    every page load and never close it."""
    from vibetrading.marketdata.providers import close_market_data_providers

    settings = Settings(_env_file=None, market_data_provider="simulated")
    try:
        first = get_market_data_provider(settings)
        second = get_market_data_provider(settings)
        assert first is second
    finally:
        await close_market_data_providers()
