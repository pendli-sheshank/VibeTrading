from __future__ import annotations

from datetime import UTC, datetime, timedelta

from vibetrading.agents.backtest.backtest_agent import BacktestAgent
from vibetrading.agents.backtest.engine import BacktestEngine
from vibetrading.agents.strategy.strategy_agent import StrategyAgent
from vibetrading.broker.mock_client import MockBrokerClient
from vibetrading.core.models import Stock
from vibetrading.llm.providers.mock_provider import MockLLMAdapter
from vibetrading.persistence.repositories import get_backtest_run, list_backtest_runs_for_stock


async def test_backtest_agent_persists_result(db_session):
    broker = MockBrokerClient(seed=3)
    strategy_agent = StrategyAgent(llm=MockLLMAdapter())  # safe-HOLD default -> zero trades, still a valid run
    engine = BacktestEngine(broker=broker, strategy_agent=strategy_agent)
    backtest_agent = BacktestAgent(engine=engine)

    stock = Stock(symbol="RELIANCE")
    end_date = datetime.now(UTC)
    start_date = end_date - timedelta(days=60)

    result = await backtest_agent.run_and_persist(db_session, 1, stock, start_date, end_date)
    await db_session.commit()

    runs = await list_backtest_runs_for_stock(db_session, 1, "RELIANCE")
    assert len(runs) == 1
    assert runs[0].stock_symbol == "RELIANCE"
    assert runs[0].total_trades == result.total_trades

    fetched = await get_backtest_run(db_session, 1, runs[0].id)
    assert fetched is not None
    assert fetched.win_rate == result.win_rate
