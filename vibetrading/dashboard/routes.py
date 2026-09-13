from __future__ import annotations

import asyncio
import logging
from datetime import UTC, datetime, timedelta

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse
from sqlalchemy.ext.asyncio import AsyncSession

from vibetrading.agents.backtest.backtest_agent import BacktestAgent
from vibetrading.agents.backtest.engine import BacktestEngine
from vibetrading.agents.research.agent import ResearchAgent
from vibetrading.agents.strategy.performance_tracker import get_performance_summary
from vibetrading.agents.strategy.strategy_agent import StrategyAgent
from vibetrading.agents.strategy.technical_agent import TechnicalAgent
from vibetrading.api.deps import get_broker, get_db
from vibetrading.auth.backend import current_dashboard_user
from vibetrading.broker.base import BrokerClient
from vibetrading.core.enums import AgentType, KillSwitchMode
from vibetrading.core.models import AgentOutput, Stock
from vibetrading.dashboard.templating import templates
from vibetrading.llm.router import LLMRouter
from vibetrading.orchestrator.event_bus import event_bus
from vibetrading.persistence.orm_models import UserORM
from vibetrading.persistence.repositories import (
    get_latest_agent_output,
    get_stock_by_symbol,
    list_recent_audit_log,
    list_recent_orders,
    list_recent_risk_events,
    list_signals_for_stock,
    list_stocks,
    save_agent_output,
)
from vibetrading.risk.config import RiskConfig
from vibetrading.risk.state import get_or_create_risk_state, set_kill_switch
from vibetrading.settings.cache import get_tenant_settings

logger = logging.getLogger(__name__)

router = APIRouter(include_in_schema=False)


def _mode(tenant_id: int) -> str:
    return get_tenant_settings(tenant_id).vibetrading_execution_mode.value


@router.get("/", response_class=HTMLResponse)
async def watchlist_page(
    request: Request, session: AsyncSession = Depends(get_db), user: UserORM = Depends(current_dashboard_user)
):
    stocks = await list_stocks(session, user.id)
    rows = []
    for stock in stocks:
        symbol = stock.symbol
        technical = await get_latest_agent_output(session, user.id, symbol, AgentType.TECHNICAL.value)
        research = await get_latest_agent_output(session, user.id, symbol, AgentType.RESEARCH.value)
        signals = await list_signals_for_stock(session, user.id, symbol)
        rows.append(
            {
                "symbol": symbol,
                "technical": technical,
                "research": research,
                "signal": signals[0] if signals else None,
            }
        )
    return templates.TemplateResponse(
        request,
        "watchlist.html",
        {
            "rows": rows,
            "mode": _mode(user.id),
            "settings": get_tenant_settings(user.id),
            "active_page": "watchlist",
        },
    )


@router.get("/stock/{symbol}", response_class=HTMLResponse)
async def stock_detail_page(
    symbol: str,
    request: Request,
    session: AsyncSession = Depends(get_db),
    user: UserORM = Depends(current_dashboard_user),
):
    symbol = symbol.upper()
    technical = await get_latest_agent_output(session, user.id, symbol, AgentType.TECHNICAL.value)
    research = await get_latest_agent_output(session, user.id, symbol, AgentType.RESEARCH.value)
    signals = await list_signals_for_stock(session, user.id, symbol)
    performance = await get_performance_summary(session, user.id, symbol)
    return templates.TemplateResponse(
        request,
        "stock_detail.html",
        {
            "symbol": symbol,
            "technical": technical,
            "research": research,
            "signals": signals[:20],
            "performance": performance,
            "mode": _mode(user.id),
            "settings": get_tenant_settings(user.id),
            "active_page": "watchlist",
        },
    )


async def _run_agent_safely(agent_type: AgentType, coro) -> AgentOutput:
    """Runs one agent's analyze() call, falling back to a zero-confidence
    AgentOutput instead of raising -- so a broker hiccup or a flaky LLM call
    on one agent never blanks out the other agent's card, and "Analyze now"
    always renders something rather than a 500."""
    try:
        return await coro
    except Exception:
        logger.exception("On-demand %s analysis failed", agent_type.value)
        return AgentOutput(
            agent_type=agent_type,
            stock_symbol="",
            timestamp=datetime.now(UTC),
            confidence=0.0,
            summary=f"{agent_type.value.title()} analysis failed -- see server logs.",
            raw_data={},
        )


@router.post("/stock/{symbol}/analyze", response_class=HTMLResponse)
async def stock_analyze(
    symbol: str,
    request: Request,
    session: AsyncSession = Depends(get_db),
    broker: BrokerClient = Depends(get_broker),
    user: UserORM = Depends(current_dashboard_user),
):
    """Runs the Research and Technical agents for one stock right now,
    on demand, instead of waiting for the next scheduled orchestrator tick --
    persists the results exactly like a scheduled cycle would, then returns
    the refreshed analysis cards."""
    symbol = symbol.upper()
    stock_orm = await get_stock_by_symbol(session, user.id, symbol)
    if stock_orm is None:
        raise HTTPException(status_code=404, detail=f"{symbol} is not on your watchlist.")
    stock = Stock(
        symbol=stock_orm.symbol,
        exchange=stock_orm.exchange,
        dhan_security_id=stock_orm.dhan_security_id,
        name=stock_orm.name,
        sector=stock_orm.sector,
    )

    settings = get_tenant_settings(user.id)
    llm_router = LLMRouter(settings)
    research_agent = ResearchAgent(llm=llm_router.get_adapter(AgentType.RESEARCH))
    technical_agent = TechnicalAgent(broker=broker)

    technical, research = await asyncio.gather(
        _run_agent_safely(AgentType.TECHNICAL, technical_agent.analyze(stock, context={})),
        _run_agent_safely(AgentType.RESEARCH, research_agent.analyze(stock, context={})),
    )
    technical.stock_symbol = symbol
    research.stock_symbol = symbol

    await save_agent_output(session, user.id, technical)
    await save_agent_output(session, user.id, research)
    await session.commit()

    for output in (technical, research):
        await event_bus.publish(
            {
                "type": "agent_output",
                "agent_type": output.agent_type.value,
                "stock_symbol": symbol,
                "confidence": output.confidence,
            },
            tenant_id=user.id,
        )

    return templates.TemplateResponse(
        request, "_stock_analysis.html", {"symbol": symbol, "technical": technical, "research": research}
    )


async def _risk_context(session: AsyncSession, tenant_id: int) -> dict:
    state = await get_or_create_risk_state(session, tenant_id)
    settings = get_tenant_settings(tenant_id)
    config = RiskConfig.from_settings(settings)
    events = await list_recent_risk_events(session, tenant_id, limit=25)
    return {
        "state": state,
        "config": config,
        "events": events,
        "mode": _mode(tenant_id),
        "settings": settings,
        "active_page": "risk",
    }


@router.get("/risk", response_class=HTMLResponse)
async def risk_page(
    request: Request, session: AsyncSession = Depends(get_db), user: UserORM = Depends(current_dashboard_user)
):
    ctx = await _risk_context(session, user.id)
    await session.commit()
    return templates.TemplateResponse(request, "risk.html", ctx)


@router.post("/risk/kill-switch", response_class=HTMLResponse)
async def risk_kill_switch_toggle(
    request: Request,
    active: bool = Form(...),
    session: AsyncSession = Depends(get_db),
    user: UserORM = Depends(current_dashboard_user),
):
    state = await set_kill_switch(
        session,
        user.id,
        active=active,
        mode=KillSwitchMode.HALT_NEW_ORDERS,
        reason="toggled from dashboard" if active else None,
    )
    await session.commit()
    await event_bus.publish(
        {"type": "kill_switch", "active": state.kill_switch_active, "mode": state.kill_switch_mode},
        tenant_id=user.id,
    )
    ctx = await _risk_context(session, user.id)
    return templates.TemplateResponse(request, "_risk_panel.html", ctx)


@router.get("/backtest", response_class=HTMLResponse)
async def backtest_page(
    request: Request, session: AsyncSession = Depends(get_db), user: UserORM = Depends(current_dashboard_user)
):
    stocks = await list_stocks(session, user.id)
    return templates.TemplateResponse(
        request,
        "backtest.html",
        {
            "watchlist": [s.symbol for s in stocks],
            "result": None,
            "mode": _mode(user.id),
            "settings": get_tenant_settings(user.id),
            "active_page": "backtest",
        },
    )


@router.post("/backtest/run", response_class=HTMLResponse)
async def backtest_run(
    request: Request,
    symbol: str = Form(...),
    days: int = Form(180),
    session: AsyncSession = Depends(get_db),
    broker: BrokerClient = Depends(get_broker),
    user: UserORM = Depends(current_dashboard_user),
):
    end_date = datetime.now(UTC)
    start_date = end_date - timedelta(days=days)

    settings = get_tenant_settings(user.id)
    llm = LLMRouter(settings).get_adapter(AgentType.BACKTEST)
    strategy_agent = StrategyAgent(llm=llm)
    engine = BacktestEngine(broker=broker, strategy_agent=strategy_agent)
    backtest_agent = BacktestAgent(engine=engine)

    result = await backtest_agent.run_and_persist(session, user.id, Stock(symbol=symbol.upper()), start_date, end_date)
    await session.commit()

    return templates.TemplateResponse(request, "_backtest_result.html", {"result": result})


@router.get("/monitor", response_class=HTMLResponse)
async def monitor_page(
    request: Request,
    session: AsyncSession = Depends(get_db),
    broker: BrokerClient = Depends(get_broker),
    user: UserORM = Depends(current_dashboard_user),
):
    positions = await broker.get_positions()
    funds = await broker.get_funds()
    orders = await list_recent_orders(session, user.id, limit=30)
    audit = await list_recent_audit_log(session, user.id, limit=30)
    return templates.TemplateResponse(
        request,
        "monitor.html",
        {
            "positions": positions,
            "funds": funds,
            "orders": orders,
            "audit": audit,
            "mode": _mode(user.id),
            "settings": get_tenant_settings(user.id),
            "active_page": "monitor",
        },
    )
