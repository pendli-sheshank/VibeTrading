"""Entrypoint: `python -m vibetrading.main` or `uvicorn vibetrading.main:app`."""

from __future__ import annotations

from vibetrading.api.app import app

if __name__ == "__main__":
    import uvicorn

    from vibetrading.config import get_settings

    settings = get_settings()
    uvicorn.run(app, host=settings.host, port=settings.port)
