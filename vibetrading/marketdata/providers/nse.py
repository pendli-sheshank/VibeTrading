from __future__ import annotations

import logging
from datetime import UTC, datetime

import httpx

from vibetrading.core.enums import DataStatus
from vibetrading.core.exceptions import MarketDataError, MarketDataUnavailableError
from vibetrading.core.models import OptionChainSnapshot, OptionStrike, Stock
from vibetrading.marketdata.providers.base import is_index

logger = logging.getLogger(__name__)

HOME_URL = "https://www.nseindia.com/option-chain"
EQUITY_URL = "https://www.nseindia.com/api/option-chain-equities"
INDEX_URL = "https://www.nseindia.com/api/option-chain-indices"

_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    ),
    "Accept": "*/*",
    "Accept-Language": "en-US,en;q=0.9",
    "Referer": HOME_URL,
}


class NseOptionChainSource:
    """Option chain (OI, change in OI, IV, PCR) from NSE's own public API.

    Free and keyed by ticker, like the rest of the analysis path. NSE serves
    this to browsers but rate-limits and blocks automated clients hard --
    datacenter IPs in particular usually get HTTP 401/403 no matter how the
    request is shaped. That is expected, not a bug to paper over: when it
    happens the snapshot shows the option-chain section as UNAVAILABLE with
    the reason, and every other section still renders.
    """

    name = "nse"

    def __init__(self, client: httpx.AsyncClient | None = None, timeout: float = 15.0):
        self._client = client or httpx.AsyncClient(
            timeout=timeout, headers=_HEADERS, follow_redirects=True
        )
        self._owns_client = client is None
        self._bootstrapped = False

    async def aclose(self) -> None:
        if self._owns_client:
            await self._client.aclose()

    async def _bootstrap(self) -> None:
        """NSE's API only answers requests carrying cookies its web page
        sets, so the page is fetched once per client first."""
        if self._bootstrapped:
            return
        try:
            await self._client.get(HOME_URL)
        except httpx.HTTPError as exc:
            raise MarketDataError(f"Could not reach nseindia.com: {exc}") from exc
        self._bootstrapped = True

    async def get_option_chain(self, stock: Stock, strikes_around_atm: int = 5) -> OptionChainSnapshot:
        await self._bootstrap()
        symbol = stock.symbol.upper().strip()
        url = INDEX_URL if is_index(stock) else EQUITY_URL

        try:
            response = await self._client.get(url, params={"symbol": symbol})
        except httpx.HTTPError as exc:
            raise MarketDataError(f"NSE option-chain request failed for {symbol}: {exc}") from exc

        if response.status_code in (401, 403):
            # Permanent from this host: retrying or waiting won't help.
            raise MarketDataUnavailableError(
                f"NSE declined the option-chain request for {symbol} (HTTP {response.status_code}). "
                "NSE blocks automated access from most server/cloud IP addresses, so option data "
                "is unavailable on this deployment. Everything else on this page is unaffected."
            )
        if response.status_code != 200:
            raise MarketDataError(f"NSE returned HTTP {response.status_code} for {symbol}.")

        try:
            payload = response.json()
        except ValueError as exc:
            raise MarketDataUnavailableError(
                f"NSE returned a non-JSON body for {symbol} (usually its bot-protection page)."
            ) from exc

        return _parse(symbol, payload, strikes_around_atm)


def _parse(symbol: str, payload: dict, strikes_around_atm: int) -> OptionChainSnapshot:
    records = payload.get("records") or {}
    rows = records.get("data") or []
    expiries = records.get("expiryDates") or []
    if not rows or not expiries:
        raise MarketDataUnavailableError(f"NSE listed no option contracts for {symbol}.")

    nearest_expiry = str(expiries[0])
    underlying = _as_float(records.get("underlyingValue"))

    parsed: list[OptionStrike] = []
    for row in rows:
        if not isinstance(row, dict) or str(row.get("expiryDate")) != nearest_expiry:
            continue
        strike = _as_float(row.get("strikePrice"))
        if strike is None:
            continue
        call = row.get("CE") if isinstance(row.get("CE"), dict) else {}
        put = row.get("PE") if isinstance(row.get("PE"), dict) else {}
        parsed.append(
            OptionStrike(
                strike=strike,
                call_oi=_as_int(call.get("openInterest")),
                call_oi_change=_as_int(call.get("changeinOpenInterest")),
                call_volume=_as_int(call.get("totalTradedVolume")),
                call_iv=_as_float(call.get("impliedVolatility")),
                call_ltp=_as_float(call.get("lastPrice")),
                put_oi=_as_int(put.get("openInterest")),
                put_oi_change=_as_int(put.get("changeinOpenInterest")),
                put_volume=_as_int(put.get("totalTradedVolume")),
                put_iv=_as_float(put.get("impliedVolatility")),
                put_ltp=_as_float(put.get("lastPrice")),
            )
        )

    if not parsed:
        raise MarketDataUnavailableError(f"NSE returned no readable strikes for {symbol}.")

    parsed.sort(key=lambda s: s.strike)
    atm = min(parsed, key=lambda s: abs(s.strike - underlying)).strike if underlying else None
    if atm is not None:
        atm_index = next(i for i, s in enumerate(parsed) if s.strike == atm)
        parsed = parsed[max(0, atm_index - strikes_around_atm) : atm_index + strikes_around_atm + 1]

    total_call_oi = sum(s.call_oi or 0 for s in parsed)
    total_put_oi = sum(s.put_oi or 0 for s in parsed)

    return OptionChainSnapshot(
        symbol=symbol,
        status=DataStatus.LIVE,
        expiry=nearest_expiry,
        underlying_price=underlying,
        atm_strike=atm,
        strikes=parsed,
        total_call_oi=total_call_oi or None,
        total_put_oi=total_put_oi or None,
        put_call_ratio=round(total_put_oi / total_call_oi, 3) if total_call_oi else None,
        timestamp=datetime.now(UTC),
        source="nse:option-chain",
    )


def _as_float(value) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _as_int(value) -> int | None:
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return None
