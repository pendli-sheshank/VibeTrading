from __future__ import annotations

import contextlib
import uuid
from pathlib import Path

from fastapi import Depends, FastAPI, Request
from fastapi.responses import RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from slowapi import _rate_limit_exceeded_handler
from slowapi.errors import RateLimitExceeded

from vibetrading.api.routers import backtest, health, monitor, risk, strategy, system, watchlist
from vibetrading.api.websocket import websocket_endpoint
from vibetrading.auth.backend import NotAuthenticated, current_active_user, current_dashboard_user
from vibetrading.auth.routes import router as auth_router
from vibetrading.dashboard.routes import router as dashboard_router
from vibetrading.dashboard.routes_settings import router as settings_router
from vibetrading.logging_conf import bind_request_id, configure_logging
from vibetrading.orchestrator.manager import MultiTenantRuntimeManager
from vibetrading.persistence.db import init_db
from vibetrading.rate_limit import limiter

STATIC_DIR = Path(__file__).resolve().parent.parent / "dashboard" / "static"


@contextlib.asynccontextmanager
async def lifespan(app: FastAPI):
    configure_logging()
    await init_db()

    manager = MultiTenantRuntimeManager()
    await manager.start_all_existing_tenants()
    manager.start_lease_loop()
    app.state.runtime_manager = manager

    try:
        yield
    finally:
        await manager.shutdown_all()


async def _not_authenticated_handler(request: Request, exc: NotAuthenticated) -> Response:
    """A human hitting a dashboard page/HTMX action while logged out gets
    sent to /login instead of a raw 401 -- an HX-Redirect for an in-flight
    HTMX request (which htmx follows as a full-page client-side redirect),
    a plain 303 for a normal page navigation."""
    if request.headers.get("HX-Request") == "true":
        return Response(status_code=200, headers={"HX-Redirect": "/login"})
    return RedirectResponse(url="/login", status_code=303)


async def _request_id_middleware(request: Request, call_next):
    """Binds a fresh request_id (see logging_conf.py) for the duration of
    every HTTP request, before any route or dependency runs, so every log
    line from a request -- across whatever it calls -- can be correlated.
    Echoed back as X-Request-ID so a client (or an upstream proxy's own
    logs) can cross-reference it too."""
    request_id = uuid.uuid4().hex
    with bind_request_id(request_id):
        response = await call_next(request)
    response.headers["X-Request-ID"] = request_id
    return response


def create_app() -> FastAPI:
    app = FastAPI(title="VibeTrading", lifespan=lifespan)

    app.state.limiter = limiter
    app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)
    app.add_exception_handler(NotAuthenticated, _not_authenticated_handler)
    app.middleware("http")(_request_id_middleware)

    app.include_router(health.router)
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
