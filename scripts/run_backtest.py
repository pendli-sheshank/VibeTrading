#!/usr/bin/env python3
"""CLI entrypoint to run a backtest over a date range.

Usage:
    python scripts/run_backtest.py RELIANCE --days 180
    python scripts/run_backtest.py TCS --start 2024-01-01 --end 2024-06-30

Uses the same BacktestEngine (and therefore the exact same StrategyAgent
code path) as the dashboard's Backtest tab. Historical candles come from
the configured Execution Agent — Dhan if DHAN_CLIENT_ID/DHAN_ACCESS_TOKEN
are set, otherwise the deterministic MockBrokerClient.
"""

from __future__ import annotations

import argparse
import asyncio
from datetime import UTC, datetime, timedelta

from vibetrading.agents.backtest.backtest_agent import BacktestAgent
from vibetrading.agents.backtest.engine import BacktestEngine
from vibetrading.agents.backtest.report import format_summary
from vibetrading.agents.strategy.strategy_agent import StrategyAgent
from vibetrading.broker.factory import get_broker_client
from vibetrading.core.enums import AgentType
from vibetrading.core.models import Stock
from vibetrading.llm.router import LLMRouter
from vibetrading.logging_conf import configure_logging
from vibetrading.persistence.db import get_session, init_db


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("symbol", help="Stock symbol, e.g. RELIANCE")
    parser.add_argument("--start", help="Start date YYYY-MM-DD (default: --days before --end)")
    parser.add_argument("--end", help="End date YYYY-MM-DD (default: today)")
    parser.add_argument("--days", type=int, default=180, help="Backtest window length if --start is omitted")
    parser.add_argument("--warmup-days", type=int, default=90, help="Extra history fetched for indicator warmup")
    parser.add_argument("--quantity", type=int, default=1, help="Fixed per-trade quantity for the simulation")
    return parser.parse_args()


async def _main() -> None:
    configure_logging()
    args = _parse_args()

    end_date = datetime.fromisoformat(args.end).replace(tzinfo=UTC) if args.end else datetime.now(UTC)
    start_date = (
        datetime.fromisoformat(args.start).replace(tzinfo=UTC)
        if args.start
        else end_date - timedelta(days=args.days)
    )

    await init_db()

    broker = get_broker_client()
    llm = LLMRouter().get_adapter(AgentType.STRATEGY)
    strategy_agent = StrategyAgent(llm=llm)
    engine = BacktestEngine(broker=broker, strategy_agent=strategy_agent, quantity=args.quantity)
    backtest_agent = BacktestAgent(engine=engine)

    stock = Stock(symbol=args.symbol)

    async with get_session() as session:
        result = await backtest_agent.run_and_persist(session, stock, start_date, end_date, warmup_days=args.warmup_days)
        await session.commit()

    print(format_summary(result))
    for trade in result.trades:
        print(
            f"  {trade.entry_timestamp.date()} -> {trade.exit_timestamp.date() if trade.exit_timestamp else '-'} "
            f"{trade.direction.upper()} entry={trade.entry_price:.2f} exit={trade.exit_price:.2f} pnl={trade.pnl:+.2f}"
        )


if __name__ == "__main__":
    asyncio.run(_main())
