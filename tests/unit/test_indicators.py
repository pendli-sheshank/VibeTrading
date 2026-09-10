from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pandas as pd
import pytest

from vibetrading.agents.strategy.technical_indicators import (
    bollinger_bands,
    candles_to_dataframe,
    compute_all_indicators,
    ema,
    macd,
    rsi,
    sma,
    support_resistance,
    volume_trend,
)
from vibetrading.core.models import Candle


def make_candles(closes: list[float], volumes: list[int] | None = None) -> list[Candle]:
    start = datetime(2024, 1, 1, tzinfo=timezone.utc)
    volumes = volumes or [100_000] * len(closes)
    candles = []
    for i, (close, vol) in enumerate(zip(closes, volumes)):
        candles.append(
            Candle(
                timestamp=start + timedelta(days=i),
                open=close,
                high=close,
                low=close,
                close=close,
                volume=vol,
            )
        )
    return candles


def test_sma_known_values():
    series = pd.Series([1, 2, 3, 4, 5], dtype=float)
    result = sma(series, window=3)
    # First two values undefined (not enough window), then (1+2+3)/3, (2+3+4)/3, (3+4+5)/3
    assert pd.isna(result.iloc[0])
    assert pd.isna(result.iloc[1])
    assert result.iloc[2] == pytest.approx(2.0)
    assert result.iloc[3] == pytest.approx(3.0)
    assert result.iloc[4] == pytest.approx(4.0)


def test_ema_known_values():
    # span=3 -> alpha = 2/(3+1) = 0.5. adjust=False recursive formula.
    series = pd.Series([1, 2, 3, 4], dtype=float)
    result = ema(series, span=3)
    alpha = 2 / (3 + 1)
    expected_0 = 1.0
    expected_1 = alpha * 2 + (1 - alpha) * expected_0
    expected_2 = alpha * 3 + (1 - alpha) * expected_1
    expected_3 = alpha * 4 + (1 - alpha) * expected_2
    assert result.iloc[2] == pytest.approx(expected_2)
    assert result.iloc[3] == pytest.approx(expected_3)


def test_rsi_all_gains_is_100():
    series = pd.Series([float(i) for i in range(1, 20)])  # strictly increasing
    result = rsi(series, period=14)
    assert result.iloc[-1] == pytest.approx(100.0)


def test_rsi_all_losses_is_0():
    series = pd.Series([float(i) for i in range(20, 1, -1)])  # strictly decreasing
    result = rsi(series, period=14)
    assert result.iloc[-1] == pytest.approx(0.0)


def test_rsi_flat_price_is_neutral_50():
    series = pd.Series([100.0] * 20)
    result = rsi(series, period=14)
    assert result.iloc[-1] == pytest.approx(50.0)


def test_rsi_bounded_0_to_100():
    series = pd.Series([100, 102, 99, 105, 103, 98, 110, 108, 95, 120, 90, 130, 140, 85, 150, 160])
    result = rsi(series, period=14).dropna()
    assert (result >= 0).all()
    assert (result <= 100).all()


def test_macd_matches_ema_difference():
    series = pd.Series([float(100 + i + (i % 5)) for i in range(40)])
    macd_line, signal_line, histogram = macd(series, fast=12, slow=26, signal=9)
    ema_fast = ema(series, 12)
    ema_slow = ema(series, 26)
    pd.testing.assert_series_equal(macd_line, ema_fast - ema_slow, check_names=False)
    pd.testing.assert_series_equal(histogram, macd_line - signal_line, check_names=False)


def test_bollinger_bands_flat_price_has_zero_width():
    series = pd.Series([50.0] * 25)
    upper, middle, lower = bollinger_bands(series, window=20, num_std=2)
    assert upper.iloc[-1] == pytest.approx(middle.iloc[-1])
    assert lower.iloc[-1] == pytest.approx(middle.iloc[-1])
    assert middle.iloc[-1] == pytest.approx(50.0)


def test_bollinger_bands_upper_above_lower_when_volatile():
    series = pd.Series([50, 55, 45, 60, 40, 58, 42, 56, 44, 52, 48, 53, 47, 51, 49, 54, 46, 57, 43, 59, 41])
    upper, middle, lower = bollinger_bands(series, window=20, num_std=2)
    assert upper.iloc[-1] > lower.iloc[-1]


def test_volume_trend_rising():
    volumes = pd.Series([100] * 20 + [500] * 5)
    result = volume_trend(volumes, short_window=5, long_window=20)
    assert result.iloc[-1] > 1.0


def test_support_resistance_rolling_window():
    highs_lows = pd.DataFrame(
        {
            "high": [10, 12, 9, 15, 11, 13, 8, 14, 10, 16, 7, 12, 9, 11, 13, 8, 14, 10, 16, 20, 5],
            "low": [8, 9, 7, 10, 9, 11, 6, 12, 8, 14, 5, 10, 7, 9, 11, 6, 12, 8, 14, 15, 3],
        }
    )
    support, resistance = support_resistance(highs_lows, window=20)
    assert resistance.iloc[19] == highs_lows["high"].iloc[0:20].max()
    assert support.iloc[19] == highs_lows["low"].iloc[0:20].min()


def test_compute_all_indicators_returns_none_when_insufficient_history():
    candles = make_candles([100.0, 101.0, 102.0])
    df = candles_to_dataframe(candles)
    result = compute_all_indicators(df)
    assert result["sma_20"] is None
    assert result["rsi_14"] is None
    assert result["close"] == pytest.approx(102.0)


def test_compute_all_indicators_full_history():
    closes = [100 + (i % 7) - 3 + i * 0.1 for i in range(60)]
    volumes = [100_000 + i * 100 for i in range(60)]
    candles = make_candles(closes, volumes)
    df = candles_to_dataframe(candles)
    result = compute_all_indicators(df)

    assert result["sma_20"] is not None
    assert result["rsi_14"] is not None
    assert 0 <= result["rsi_14"] <= 100
    assert result["macd_line"] is not None
    assert result["bollinger_upper"] >= result["bollinger_lower"]
