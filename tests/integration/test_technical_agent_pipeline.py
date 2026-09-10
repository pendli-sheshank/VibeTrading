from __future__ import annotations

from vibetrading.agents.strategy.technical_agent import TechnicalAgent
from vibetrading.broker.mock_client import MockBrokerClient
from vibetrading.core.enums import AgentType
from vibetrading.core.models import Stock
from vibetrading.persistence.repositories import get_latest_agent_output, save_agent_output


async def test_technical_agent_produces_and_persists_agent_output(db_session):
    broker = MockBrokerClient(seed=7)
    agent = TechnicalAgent(broker=broker, lookback_days=120)
    stock = Stock(symbol="RELIANCE")

    output = await agent.analyze(stock, context={})

    assert output.agent_type == AgentType.TECHNICAL
    assert output.stock_symbol == "RELIANCE"
    assert 0.0 <= output.confidence <= 1.0
    assert output.summary
    assert "rsi_14" in output.raw_data

    await save_agent_output(db_session, output)
    await db_session.commit()

    persisted = await get_latest_agent_output(db_session, "RELIANCE", AgentType.TECHNICAL.value)
    assert persisted is not None
    assert persisted.stock_symbol == "RELIANCE"
    assert persisted.summary == output.summary
    assert persisted.raw_data["rsi_14"] == output.raw_data["rsi_14"]


async def test_technical_agent_is_deterministic_for_same_seed():
    broker_a = MockBrokerClient(seed=99)
    broker_b = MockBrokerClient(seed=99)
    stock = Stock(symbol="TCS")

    output_a = await TechnicalAgent(broker=broker_a).analyze(stock, context={})
    output_b = await TechnicalAgent(broker=broker_b).analyze(stock, context={})

    assert output_a.raw_data == output_b.raw_data


async def test_mock_broker_place_order_updates_positions_and_funds():
    from vibetrading.core.enums import ExecutionMode, OrderSide
    from vibetrading.core.models import OrderRequest
    from vibetrading.risk.tokens import mint_token

    broker = MockBrokerClient(seed=5, initial_funds=100_000.0)
    stock = Stock(symbol="INFY")
    ltp = await broker.get_ltp(stock)

    order = OrderRequest(
        stock_symbol="INFY", side=OrderSide.BUY, quantity=10, mode=ExecutionMode.PAPER
    )
    token = mint_token(signal_id=None, stock_symbol="INFY", quantity=10)
    result = await broker.place_order(order, token)

    assert result.filled_quantity == 10
    assert result.filled_price == ltp

    positions = await broker.get_positions()
    assert len(positions) == 1
    assert positions[0].stock_symbol == "INFY"
    assert positions[0].quantity == 10

    funds = await broker.get_funds()
    assert funds.available_balance == 100_000.0 - (ltp * 10)
