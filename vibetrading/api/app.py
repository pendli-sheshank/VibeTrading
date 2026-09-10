from __future__ import annotations

import contextlib
from pathlib import Path

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

from vibetrading.api.routers import backtest, monitor, risk, strategy, system, watchlist
from vibetrading.api.websocket import run_event_forwarder, websocket_endpoint
from vibetrading.dashboard.routes import router as dashboard_router
from vibetrading.logging_conf import configure_logging
from vibetrading.persistence.db import init_db

STATIC_DIR = Path(__file__).resolve().parent.parent / "dashboard" / "static"


@contextlib.asynccontextmanager
async def lifespan(app: FastAPI):
    configure_logging()
    await init_db()
    async with run_event_forwarder():
        yield


def create_app() -> FastAPI:
    app = FastAPI(title="VibeTrading", lifespan=lifespan)

    app.include_router(watchlist.router)
    app.include_router(strategy.router)
    app.include_router(risk.router)
    app.include_router(backtest.router)
    app.include_router(monitor.router)
    app.include_router(system.router)
    app.add_api_websocket_route("/ws", websocket_endpoint)
    app.include_router(dashboard_router)

    if STATIC_DIR.exists():
        app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")

    return app


app = create_app()
