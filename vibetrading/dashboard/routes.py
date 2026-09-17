from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse
from sqlalchemy.ext.asyncio import AsyncSession

from vibetrading.agents.backtest.backtest_agent import BacktestAgent
from vibetrading.agents.backtest.engine import BacktestEngine
from vibetrading.agents.on_demand import analyze_stock
from vibetrading.agents.strategy.performance_tracker import get_performance_summary
from vibetrading.agents.strategy.strategy_agent import StrategyAgent
from vibetrading.api.deps import get_broker, get_db, get_market_data
from vibetrading.auth.backend import current_dashboard_user
from vibetrading.broker.base import BrokerClient
from vibetrading.core.enums import AgentType, KillSwitchMode
from vibetrading.core.exceptions import BrokerError, MarketDataUnavailableError
from vibetrading.core.models import Stock
from vibetrading.core.reliability import CircuitBreakerOpenError
from vibetrading.dashboard.templating import templates
from vibetrading.llm.router import LLMRouter
from vibetrading.marketdata import build_market_snapshot
from vibetrading.marketdata.providers import MarketDataProvider
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
)
from vibetrading.risk.config import RiskConfig
from vibetrading.risk.state import get_or_create_risk_state, set_kill_switch
from vibetrading.settings.cache import get_tenant_settings

logger = logging.getLogger(__name__)

# Bounds the Backtest form's own input element already enforces client-side;
# re-checked here because a hand-rolled POST bypasses the browser entirely,
# and an unbounded range is a slow query, not a useful backtest.
MIN_BACKTEST_DAYS = 30
MAX_BACKTEST_DAYS = 1825

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
            "result": None,
            "signals": signals[:20],
            "performance": performance,
            "mode": _mode(user.id),
            "settings": get_tenant_settings(user.id),
            "active_page": "watchlist",
        },
    )


async def _resolve_watchlist_stock(session: AsyncSession, tenant_id: int, symbol: str) -> Stock:
    """The watchlist row for a symbol, as a domain Stock — including the Dhan
    security ID, without which a live broker cannot be asked about it."""
    stock_orm = await get_stock_by_symbol(session, tenant_id, symbol)
    if stock_orm is None:
        raise HTTPException(status_code=404, detail=f"{symbol} is not on your watchlist.")
    return Stock(
        symbol=stock_orm.symbol,
        exchange=stock_orm.exchange,
        dhan_security_id=stock_orm.dhan_security_id,
        name=stock_orm.name,
        sector=stock_orm.sector,
    )


@router.get("/stock/{symbol}/market-data", response_class=HTMLResponse)
async def stock_market_data(
    symbol: str,
    request: Request,
    session: AsyncSession = Depends(get_db),
    market_data: MarketDataProvider = Depends(get_market_data),
    user: UserORM = Depends(current_dashboard_user),
):
    """The live market data an analysis would run on, rendered before any
    analysis happens — so the inputs are visible and their freshness/source
    is stated up front."""
    symbol = symbol.upper()
    stock = await _resolve_watchlist_stock(session, user.id, symbol)
    snapshot = await build_market_snapshot(market_data, stock)
    return templates.TemplateResponse(
        request, "_market_data.html", {"symbol": symbol, "snapshot": snapshot}
    )


@router.post("/stock/{symbol}/analyze", response_class=HTMLResponse)
async def stock_analyze(
    symbol: str,
    request: Request,
    session: AsyncSession = Depends(get_db),
    market_data: MarketDataProvider = Depends(get_market_data),
    user: UserORM = Depends(current_dashboard_user),
):
    """Runs the Research and Technical agents for one stock right now, on
    demand, instead of waiting for the next scheduled orchestrator tick.

    Always renders a fragment htmx can swap in — a result, a
    DATA_INSUFFICIENT notice, or an error with the reason — never a bare 500
    whose body htmx silently discards.
    """
    symbol = symbol.upper()
    stock = await _resolve_watchlist_stock(session, user.id, symbol)

    result = await analyze_stock(
        market_data=market_data,
        settings=get_tenant_settings(user.id),
        stock=stock,
        session=session,
        tenant_id=user.id,
    )
    await session.commit()

    for output in (result.technical, result.research):
        if output is not None:
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
        request,
        "_stock_analysis.html",
        {
            "symbol": symbol,
            "result": result,
            "snapshot": result.snapshot,
            "technical": result.technical,
            "research": result.research,
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
    market_data: MarketDataProvider = Depends(get_market_data),
    user: UserORM = Depends(current_dashboard_user),
):
    """Run a backtest and render the result, or render why it couldn't run.

    Everything that used to reach the client as a 500 (an unmapped security
    ID, a Dhan API rejection, a date range with no candles in it) is now
    either rejected up front with a validation message or caught here and
    rendered as an error card with the actual reason.
    """
    symbol = symbol.upper()

    if not (MIN_BACKTEST_DAYS <= days <= MAX_BACKTEST_DAYS):
        return _backtest_error(
            request,
            symbol,
            f"Days must be between {MIN_BACKTEST_DAYS} and {MAX_BACKTEST_DAYS} — got {days}.",
            status_code=400,
        )

    stock_orm = await get_stock_by_symbol(session, user.id, symbol)
    if stock_orm is None:
        return _backtest_error(
            request, symbol, f"{symbol} is not on your watchlist, so there is nothing to backtest.", status_code=404
        )
    stock = Stock(
        symbol=stock_orm.symbol,
        exchange=stock_orm.exchange,
        dhan_security_id=stock_orm.dhan_security_id,
    )

    end_date = datetime.now(UTC)
    start_date = end_date - timedelta(days=days)

    settings = get_tenant_settings(user.id)
    llm = LLMRouter(settings).get_adapter(AgentType.BACKTEST)
    engine = BacktestEngine(market_data=market_data, strategy_agent=StrategyAgent(llm=llm))
    backtest_agent = BacktestAgent(engine=engine)

    try:
        result = await backtest_agent.run_and_persist(session, user.id, stock, start_date, end_date)
    except MarketDataUnavailableError as exc:
        return _backtest_error(request, symbol, str(exc), status_code=200, status_label="DATA_INSUFFICIENT")
    except CircuitBreakerOpenError as exc:
        # The broker has been failing repeatedly and calls are being skipped
        # to let it recover. Temporary, and worth saying so plainly.
        return _backtest_error(request, symbol, str(exc), status_code=503, status_label="BROKER_UNAVAILABLE")
    except BrokerError as exc:
        logger.warning("Backtest for %s failed at the broker: %s", symbol, exc)
        return _backtest_error(request, symbol, str(exc), status_code=502)

    await session.commit()
    return templates.TemplateResponse(request, "_backtest_result.html", {"result": result})


def _backtest_error(
    request: Request, symbol: str, message: str, status_code: int, status_label: str = "ERROR"
) -> HTMLResponse:
    return templates.TemplateResponse(
        request,
        "_backtest_error.html",
        {"symbol": symbol, "message": message, "status_label": status_label},
        status_code=status_code,
    )


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
