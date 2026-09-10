from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.ext.asyncio import AsyncSession

from vibetrading.agents.backtest.backtest_agent import BacktestAgent
from vibetrading.agents.backtest.engine import BacktestEngine
from vibetrading.agents.strategy.performance_tracker import get_performance_summary
from vibetrading.agents.strategy.strategy_agent import StrategyAgent
from vibetrading.api.deps import get_broker, get_db
from vibetrading.broker.base import BrokerClient
from vibetrading.config import get_settings
from vibetrading.core.enums import AgentType, KillSwitchMode
from vibetrading.core.models import Stock
from vibetrading.llm.router import LLMRouter
from vibetrading.orchestrator.event_bus import event_bus
from vibetrading.persistence.repositories import (
    get_latest_agent_output,
    list_recent_audit_log,
    list_recent_orders,
    list_recent_risk_events,
    list_signals_for_stock,
)
from vibetrading.risk.config import RiskConfig
from vibetrading.risk.state import get_or_create_risk_state, set_kill_switch

TEMPLATES_DIR = Path(__file__).resolve().parent / "templates"
templates = Jinja2Templates(directory=str(TEMPLATES_DIR))

router = APIRouter(include_in_schema=False)


def _mode() -> str:
    return get_settings().vibetrading_execution_mode.value


@router.get("/", response_class=HTMLResponse)
async def watchlist_page(request: Request, session: AsyncSession = Depends(get_db)):
    settings = get_settings()
    rows = []
    for symbol in settings.watchlist_symbols:
        technical = await get_latest_agent_output(session, symbol, AgentType.TECHNICAL.value)
        research = await get_latest_agent_output(session, symbol, AgentType.RESEARCH.value)
        signals = await list_signals_for_stock(session, symbol)
        rows.append(
            {
                "symbol": symbol,
                "technical": technical,
                "research": research,
                "signal": signals[0] if signals else None,
            }
        )
    return templates.TemplateResponse(
        request, "watchlist.html", {"rows": rows, "mode": _mode(), "active_page": "watchlist"}
    )


@router.get("/stock/{symbol}", response_class=HTMLResponse)
async def stock_detail_page(symbol: str, request: Request, session: AsyncSession = Depends(get_db)):
    symbol = symbol.upper()
    technical = await get_latest_agent_output(session, symbol, AgentType.TECHNICAL.value)
    research = await get_latest_agent_output(session, symbol, AgentType.RESEARCH.value)
    signals = await list_signals_for_stock(session, symbol)
    performance = await get_performance_summary(session, symbol)
    return templates.TemplateResponse(
        request,
        "stock_detail.html",
        {
            "symbol": symbol,
            "technical": technical,
            "research": research,
            "signals": signals[:20],
            "performance": performance,
            "mode": _mode(),
            "active_page": "watchlist",
        },
    )


async def _risk_context(session: AsyncSession) -> dict:
    state = await get_or_create_risk_state(session)
    settings = get_settings()
    config = RiskConfig.from_settings(settings)
    events = await list_recent_risk_events(session, limit=25)
    return {"state": state, "config": config, "events": events, "mode": _mode(), "active_page": "risk"}


@router.get("/risk", response_class=HTMLResponse)
async def risk_page(request: Request, session: AsyncSession = Depends(get_db)):
    ctx = await _risk_context(session)
    await session.commit()
    return templates.TemplateResponse(request, "risk.html", ctx)


@router.post("/risk/kill-switch", response_class=HTMLResponse)
async def risk_kill_switch_toggle(
    request: Request, active: bool = Form(...), session: AsyncSession = Depends(get_db)
):
    state = await set_kill_switch(
        session,
        active=active,
        mode=KillSwitchMode.HALT_NEW_ORDERS,
        reason="toggled from dashboard" if active else None,
    )
    await session.commit()
    await event_bus.publish(
        {"type": "kill_switch", "active": state.kill_switch_active, "mode": state.kill_switch_mode}
    )
    ctx = await _risk_context(session)
    return templates.TemplateResponse(request, "_risk_panel.html", ctx)


@router.get("/backtest", response_class=HTMLResponse)
async def backtest_page(request: Request):
    settings = get_settings()
    return templates.TemplateResponse(
        request,
        "backtest.html",
        {"watchlist": settings.watchlist_symbols, "result": None, "mode": _mode(), "active_page": "backtest"},
    )


@router.post("/backtest/run", response_class=HTMLResponse)
async def backtest_run(
    request: Request,
    symbol: str = Form(...),
    days: int = Form(180),
    session: AsyncSession = Depends(get_db),
    broker: BrokerClient = Depends(get_broker),
):
    end_date = datetime.now(UTC)
    start_date = end_date - timedelta(days=days)

    llm = LLMRouter().get_adapter(AgentType.BACKTEST)
    strategy_agent = StrategyAgent(llm=llm)
    engine = BacktestEngine(broker=broker, strategy_agent=strategy_agent)
    backtest_agent = BacktestAgent(engine=engine)

    result = await backtest_agent.run_and_persist(session, Stock(symbol=symbol.upper()), start_date, end_date)
    await session.commit()

    return templates.TemplateResponse(request, "_backtest_result.html", {"result": result})


@router.get("/monitor", response_class=HTMLResponse)
async def monitor_page(
    request: Request, session: AsyncSession = Depends(get_db), broker: BrokerClient = Depends(get_broker)
):
    positions = await broker.get_positions()
    funds = await broker.get_funds()
    orders = await list_recent_orders(session, limit=30)
    audit = await list_recent_audit_log(session, limit=30)
    return templates.TemplateResponse(
        request,
        "monitor.html",
        {
            "positions": positions,
            "funds": funds,
            "orders": orders,
            "audit": audit,
            "mode": _mode(),
            "active_page": "monitor",
        },
    )
