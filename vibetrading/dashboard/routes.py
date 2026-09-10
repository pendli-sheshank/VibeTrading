from __future__ import annotations

from datetime import UTC, datetime, timedelta

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse
from sqlalchemy.ext.asyncio import AsyncSession

from vibetrading.agents.backtest.backtest_agent import BacktestAgent
from vibetrading.agents.backtest.engine import BacktestEngine
from vibetrading.agents.strategy.performance_tracker import get_performance_summary
from vibetrading.agents.strategy.strategy_agent import StrategyAgent
from vibetrading.api.deps import get_broker, get_db
from vibetrading.auth.backend import current_dashboard_user
from vibetrading.broker.base import BrokerClient
from vibetrading.core.enums import AgentType, KillSwitchMode
from vibetrading.core.models import Stock
from vibetrading.dashboard.templating import templates
from vibetrading.llm.router import LLMRouter
from vibetrading.orchestrator.event_bus import event_bus
from vibetrading.persistence.orm_models import UserORM
from vibetrading.persistence.repositories import (
    get_latest_agent_output,
    list_recent_audit_log,
    list_recent_orders,
    list_recent_risk_events,
    list_signals_for_stock,
    list_stocks,
)
from vibetrading.risk.config import RiskConfig
from vibetrading.risk.state import get_or_create_risk_state, set_kill_switch
from vibetrading.settings.cache import get_tenant_settings

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
        {"type": "kill_switch", "active": state.kill_switch_active, "mode": state.kill_switch_mode}
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
