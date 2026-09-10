from __future__ import annotations

from vibetrading.config import Settings, get_settings
from vibetrading.core.models import Stock


def get_watchlist(settings: Settings | None = None) -> list[Stock]:
    """The configured stock universe. env-list for v1 (WATCHLIST=A,B,C);
    a DB-backed table is a natural later upgrade if the list needs to be
    editable at runtime without a redeploy.
    """
    settings = settings or get_settings()
    return [Stock(symbol=symbol) for symbol in settings.watchlist_symbols]
