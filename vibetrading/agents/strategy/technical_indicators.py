from __future__ import annotations

import numpy as np
import pandas as pd

from vibetrading.core.models import Candle


def candles_to_dataframe(candles: list[Candle]) -> pd.DataFrame:
    df = pd.DataFrame(
        {
            "timestamp": [c.timestamp for c in candles],
            "open": [c.open for c in candles],
            "high": [c.high for c in candles],
            "low": [c.low for c in candles],
            "close": [c.close for c in candles],
            "volume": [c.volume for c in candles],
        }
    )
    return df.set_index("timestamp").sort_index()


def sma(series: pd.Series, window: int) -> pd.Series:
    return series.rolling(window=window, min_periods=window).mean()


def ema(series: pd.Series, span: int) -> pd.Series:
    return series.ewm(span=span, adjust=False, min_periods=span).mean()


def rsi(series: pd.Series, period: int = 14) -> pd.Series:
    """Wilder's RSI. Values range 0-100; >70 conventionally overbought, <30 oversold."""
    delta = series.diff()
    gain = delta.clip(lower=0)
    loss = -delta.clip(upper=0)

    avg_gain = gain.ewm(alpha=1 / period, adjust=False, min_periods=period).mean()
    avg_loss = loss.ewm(alpha=1 / period, adjust=False, min_periods=period).mean()

    with np.errstate(divide="ignore", invalid="ignore"):
        rs = avg_gain / avg_loss
    result = 100 - (100 / (1 + rs))

    # Edge cases division can't express cleanly: no losses at all -> RSI 100,
    # no gains at all -> RSI 0, no movement whatsoever -> RSI 50 (neutral).
    result = result.where(avg_loss != 0, other=100.0)
    result = result.where(avg_gain != 0, other=0.0)
    result = result.where(~((avg_gain == 0) & (avg_loss == 0)), other=50.0)
    return result


def macd(
    series: pd.Series, fast: int = 12, slow: int = 26, signal: int = 9
) -> tuple[pd.Series, pd.Series, pd.Series]:
    ema_fast = ema(series, fast)
    ema_slow = ema(series, slow)
    macd_line = ema_fast - ema_slow
    signal_line = macd_line.ewm(span=signal, adjust=False, min_periods=signal).mean()
    histogram = macd_line - signal_line
    return macd_line, signal_line, histogram


def bollinger_bands(
    series: pd.Series, window: int = 20, num_std: float = 2.0
) -> tuple[pd.Series, pd.Series, pd.Series]:
    middle = sma(series, window)
    std = series.rolling(window=window, min_periods=window).std(ddof=0)
    upper = middle + num_std * std
    lower = middle - num_std * std
    return upper, middle, lower


def volume_trend(volume: pd.Series, short_window: int = 5, long_window: int = 20) -> pd.Series:
    """Ratio of recent average volume to longer-window average volume. >1 means rising activity."""
    short_avg = volume.rolling(window=short_window, min_periods=short_window).mean()
    long_avg = volume.rolling(window=long_window, min_periods=long_window).mean()
    return (short_avg / long_avg.replace(0, pd.NA)).fillna(1.0)


def support_resistance(df: pd.DataFrame, window: int = 20) -> tuple[pd.Series, pd.Series]:
    """Rolling window support (recent low) and resistance (recent high)."""
    support = df["low"].rolling(window=window, min_periods=window).min()
    resistance = df["high"].rolling(window=window, min_periods=window).max()
    return support, resistance


def compute_all_indicators(df: pd.DataFrame) -> dict:
    """Compute every indicator and return the latest (most recent) value of each,
    plus enough context (previous RSI, MACD histogram trend) for interpretation.
    Returns an empty-ish dict of None values if there isn't enough history yet.
    """
    close = df["close"]

    sma_20 = sma(close, 20)
    sma_50 = sma(close, 50)
    ema_12 = ema(close, 12)
    ema_26 = ema(close, 26)
    rsi_14 = rsi(close, 14)
    macd_line, macd_signal, macd_hist = macd(close)
    bb_upper, bb_middle, bb_lower = bollinger_bands(close)
    vol_trend = volume_trend(df["volume"])
    support, resistance = support_resistance(df)

    def last(series: pd.Series) -> float | None:
        if series.empty or pd.isna(series.iloc[-1]):
            return None
        return float(series.iloc[-1])

    return {
        "close": last(close),
        "sma_20": last(sma_20),
        "sma_50": last(sma_50),
        "ema_12": last(ema_12),
        "ema_26": last(ema_26),
        "rsi_14": last(rsi_14),
        "rsi_14_prev": last(rsi_14.iloc[:-1]) if len(rsi_14) > 1 else None,
        "macd_line": last(macd_line),
        "macd_signal": last(macd_signal),
        "macd_histogram": last(macd_hist),
        "macd_histogram_prev": last(macd_hist.iloc[:-1]) if len(macd_hist) > 1 else None,
        "bollinger_upper": last(bb_upper),
        "bollinger_middle": last(bb_middle),
        "bollinger_lower": last(bb_lower),
        "volume_trend": last(vol_trend),
        "support": last(support),
        "resistance": last(resistance),
    }
