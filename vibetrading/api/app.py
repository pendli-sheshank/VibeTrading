from __future__ import annotations

import contextlib
from pathlib import Path

from fastapi import Depends, FastAPI, Request
from fastapi.responses import RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from slowapi import _rate_limit_exceeded_handler
from slowapi.errors import RateLimitExceeded

from vibetrading.api.routers import backtest, monitor, risk, strategy, system, watchlist
from vibetrading.api.websocket import run_event_forwarder, websocket_endpoint
from vibetrading.auth.backend import NotAuthenticated, current_active_user, current_dashboard_user
from vibetrading.auth.routes import router as auth_router
from vibetrading.dashboard.routes import router as dashboard_router
from vibetrading.dashboard.routes_settings import router as settings_router
from vibetrading.logging_conf import configure_logging
from vibetrading.orchestrator.runtime import OrchestratorRuntime
from vibetrading.persistence.db import get_session, init_db
from vibetrading.persistence.repositories import seed_default_watchlist_if_empty
from vibetrading.rate_limit import limiter
from vibetrading.settings.service import load_settings_from_db

STATIC_DIR = Path(__file__).resolve().parent.parent / "dashboard" / "static"


@contextlib.asynccontextmanager
async def lifespan(app: FastAPI):
    configure_logging()
    await init_db()

    async with get_session() as session:
        await load_settings_from_db(session)
        await seed_default_watchlist_if_empty(session)
        await session.commit()

    runtime = OrchestratorRuntime()
    await runtime.start()
    app.state.runtime = runtime

    async with run_event_forwarder():
        try:
            yield
        finally:
            await runtime.shutdown()


async def _not_authenticated_handler(request: Request, exc: NotAuthenticated) -> Response:
    """A human hitting a dashboard page/HTMX action while logged out gets
    sent to /login instead of a raw 401 -- an HX-Redirect for an in-flight
    HTMX request (which htmx follows as a full-page client-side redirect),
    a plain 303 for a normal page navigation."""
    if request.headers.get("HX-Request") == "true":
        return Response(status_code=200, headers={"HX-Redirect": "/login"})
    return RedirectResponse(url="/login", status_code=303)


def create_app() -> FastAPI:
    app = FastAPI(title="VibeTrading", lifespan=lifespan)

    app.state.limiter = limiter
    app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)
    app.add_exception_handler(NotAuthenticated, _not_authenticated_handler)

    app.include_router(auth_router)

    api_auth = [Depends(current_active_user)]
    app.include_router(watchlist.router, dependencies=api_auth)
    app.include_router(strategy.router, dependencies=api_auth)
    app.include_router(risk.router, dependencies=api_auth)
    app.include_router(backtest.router, dependencies=api_auth)
    app.include_router(monitor.router, dependencies=api_auth)
    app.include_router(system.router, dependencies=api_auth)
    app.add_api_websocket_route("/ws", websocket_endpoint)

    dashboard_auth = [Depends(current_dashboard_user)]
    app.include_router(dashboard_router, dependencies=dashboard_auth)
    app.include_router(settings_router, dependencies=dashboard_auth)

    if STATIC_DIR.exists():
        app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")

    return app


app = create_app()
