from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse, PlainTextResponse
from sqlalchemy.ext.asyncio import AsyncSession

from vibetrading.api.deps import get_db, get_runtime
from vibetrading.config import get_settings
from vibetrading.core.models import Stock
from vibetrading.dashboard.templating import templates
from vibetrading.orchestrator.runtime import OrchestratorRuntime, should_restart
from vibetrading.persistence.repositories import delete_stock, list_stocks, upsert_stock
from vibetrading.risk.config import RiskConfig
from vibetrading.settings.registry import SECTIONS, fields_in_section
from vibetrading.settings.service import clear_secret, save_settings

router = APIRouter(include_in_schema=False)

# vibetrading_execution_mode lives in the execution_broker section of the
# registry (so it's still validated/round-tripped like any other setting),
# but it's never edited through the generic per-section save form below —
# only through the dedicated, confirmation-gated execution-mode routes,
# via the _mode_control.html component embedded in that section's partial.
_MODE_KEY = "vibetrading_execution_mode"


def _mode() -> str:
    return get_settings().vibetrading_execution_mode.value


def _stock_from_form(symbol: str, form) -> Stock:
    def _opt(name: str) -> str | None:
        raw = form.get(name)
        return raw.strip() or None if raw else None

    return Stock(
        symbol=symbol,
        exchange=str(form.get("exchange") or "NSE"),
        dhan_security_id=_opt("dhan_security_id"),
        name=_opt("name"),
        sector=_opt("sector"),
    )


async def _try_restart(runtime: OrchestratorRuntime) -> PlainTextResponse | None:
    """Applies a settings change that needs a restart. The settings save
    itself has already been committed by the time this runs — a failure
    here (a typo'd Dhan credential, the dhanhq extra not being installed,
    etc.) means the new settings couldn't be *applied*, not that they were
    lost, and OrchestratorRuntime.restart() guarantees the previous
    broker/scheduler is left running untouched rather than the app being
    left without any broker at all. Returns a response to send back
    instead of the normal partial when that happens, or None on success.
    """
    try:
        await runtime.restart()
    except Exception as exc:  # noqa: BLE001 - deliberately broad: any rebuild failure here
        # must leave the previous broker/scheduler running rather than crash the request.
        return PlainTextResponse(
            f"Settings were saved, but couldn't be applied: {exc} "
            "The previous configuration is still running.",
            status_code=502,
        )
    return None


async def _settings_context(session: AsyncSession) -> dict[str, Any]:
    settings = get_settings()
    stocks = await list_stocks(session)
    return {
        "mode": _mode(),
        "active_page": "settings",
        "settings": settings,
        "stocks": stocks,
        "config": RiskConfig.from_settings(settings),
        "sections": {
            "execution_broker": [f for f in fields_in_section("execution_broker") if f.key != _MODE_KEY],
            "llm": fields_in_section("llm"),
            "risk_limits": fields_in_section("risk_limits"),
            "data_sources": fields_in_section("data_sources"),
        },
    }


@router.get("/settings", response_class=HTMLResponse)
async def settings_page(request: Request, session: AsyncSession = Depends(get_db)):
    ctx = await _settings_context(session)
    return templates.TemplateResponse(request, "settings.html", ctx)


@router.post("/settings/save/{section}", response_class=HTMLResponse)
async def save_settings_section(
    section: str,
    request: Request,
    session: AsyncSession = Depends(get_db),
    runtime: OrchestratorRuntime = Depends(get_runtime),
):
    if section not in SECTIONS:
        return PlainTextResponse("Unknown settings section.", status_code=404)

    form = await request.form()
    fields = [f for f in fields_in_section(section) if f.key != _MODE_KEY]

    updates: dict[str, Any] = {}
    changed_keys: set[str] = set()

    for field in fields:
        if field.secret and form.get(f"clear__{field.key}") == "on":
            await clear_secret(session, field.key)
            changed_keys.add(field.key)
            continue

        raw = form.get(field.key)

        if field.type.__name__ == "bool":
            updates[field.key] = raw is not None
            continue

        if raw is None or str(raw).strip() == "":
            continue  # blank -> leave unchanged (also save_settings()'s rule for secrets)

        try:
            if field.type.__name__ == "int":
                updates[field.key] = int(raw)
            elif field.type.__name__ == "float":
                updates[field.key] = float(raw)
            else:
                updates[field.key] = raw
        except ValueError:
            continue  # unparseable input -> leave that one field unchanged rather than 500

    changed_keys |= await save_settings(session, updates)
    await session.commit()

    if should_restart(changed_keys):
        error = await _try_restart(runtime)
        if error is not None:
            return error

    ctx = await _settings_context(session)
    return templates.TemplateResponse(request, f"_settings_{section}.html", ctx)


@router.get("/settings/mode-control", response_class=HTMLResponse)
async def mode_control(request: Request, dom_id: str = "nav-mode-control"):
    return templates.TemplateResponse(request, "_mode_control.html", {"dom_id": dom_id})


@router.get("/settings/mode-control/live-confirm", response_class=HTMLResponse)
async def mode_control_live_confirm(request: Request, dom_id: str = "nav-mode-control"):
    config = RiskConfig.from_settings(get_settings())
    return templates.TemplateResponse(request, "_mode_live_confirm.html", {"dom_id": dom_id, "config": config})


@router.post("/settings/execution-mode/paper", response_class=HTMLResponse)
async def switch_to_paper(
    request: Request,
    session: AsyncSession = Depends(get_db),
    runtime: OrchestratorRuntime = Depends(get_runtime),
):
    form = await request.form()
    dom_id = form.get("dom_id", "nav-mode-control")

    changed = await save_settings(session, {_MODE_KEY: "paper"})
    await session.commit()
    if should_restart(changed):
        error = await _try_restart(runtime)
        if error is not None:
            return error

    return templates.TemplateResponse(request, "_mode_control.html", {"dom_id": dom_id})


@router.post("/settings/execution-mode/live", response_class=HTMLResponse)
async def switch_to_live(
    request: Request,
    session: AsyncSession = Depends(get_db),
    runtime: OrchestratorRuntime = Depends(get_runtime),
):
    """The one server-side-enforced gate for entering live trading: a save
    that flips vibetrading_execution_mode to "live" is only ever applied
    here, and only when confirm_live == "yes" was actually submitted --
    never as a side effect of the generic per-section settings form above.
    Anything else (missing/garbled confirm_live) is rejected with 400 and
    the mode is left exactly as it was.
    """
    form = await request.form()
    dom_id = form.get("dom_id", "nav-mode-control")

    if form.get("confirm_live") != "yes":
        return PlainTextResponse("Switching to live mode requires explicit confirmation.", status_code=400)

    changed = await save_settings(session, {_MODE_KEY: "live"})
    await session.commit()
    if should_restart(changed):
        error = await _try_restart(runtime)
        if error is not None:
            return error

    return templates.TemplateResponse(request, "_mode_control.html", {"dom_id": dom_id})


@router.post("/settings/watchlist/add", response_class=HTMLResponse)
async def watchlist_add(
    request: Request,
    session: AsyncSession = Depends(get_db),
    runtime: OrchestratorRuntime = Depends(get_runtime),
):
    form = await request.form()
    symbol = str(form.get("symbol", "")).strip()
    if not symbol:
        return PlainTextResponse("A stock symbol is required.", status_code=400)

    await upsert_stock(session, _stock_from_form(symbol, form))
    await session.commit()
    error = await _try_restart(runtime)
    if error is not None:
        return error

    ctx = await _settings_context(session)
    return templates.TemplateResponse(request, "_settings_watchlist.html", ctx)


@router.post("/settings/watchlist/update/{symbol}", response_class=HTMLResponse)
async def watchlist_update(
    symbol: str,
    request: Request,
    session: AsyncSession = Depends(get_db),
    runtime: OrchestratorRuntime = Depends(get_runtime),
):
    form = await request.form()
    await upsert_stock(session, _stock_from_form(symbol, form))
    await session.commit()
    error = await _try_restart(runtime)
    if error is not None:
        return error

    ctx = await _settings_context(session)
    return templates.TemplateResponse(request, "_settings_watchlist.html", ctx)


@router.post("/settings/watchlist/delete/{symbol}", response_class=HTMLResponse)
async def watchlist_delete(
    symbol: str,
    request: Request,
    session: AsyncSession = Depends(get_db),
    runtime: OrchestratorRuntime = Depends(get_runtime),
):
    await delete_stock(session, symbol)
    await session.commit()
    error = await _try_restart(runtime)
    if error is not None:
        return error

    ctx = await _settings_context(session)
    return templates.TemplateResponse(request, "_settings_watchlist.html", ctx)
