from __future__ import annotations

from vibetrading.core.models import BacktestResult


def format_summary(result: BacktestResult) -> str:
    """Human-readable one-paragraph summary, used by the CLI and the
    Backtest dashboard page."""
    if result.total_trades == 0:
        return (
            f"{result.stock_symbol}: no trades triggered between "
            f"{result.start_date.date()} and {result.end_date.date()}."
        )
    return (
        f"{result.stock_symbol} {result.start_date.date()} -> {result.end_date.date()}: "
        f"{result.total_trades} trades, {result.win_rate:.1%} win rate, "
        f"total P&L {result.total_pnl:+.2f}, max drawdown {result.max_drawdown:.2f}."
    )
