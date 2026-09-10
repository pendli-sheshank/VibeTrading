from __future__ import annotations

import os

from vibetrading.config import Settings
from vibetrading.core.enums import ExecutionMode


def test_defaults_are_safe_paper_mode():
    settings = Settings(_env_file=None)
    assert settings.vibetrading_execution_mode == ExecutionMode.PAPER
    assert settings.vibetrading_kill_switch is False
    assert settings.has_dhan_credentials is False


def test_watchlist_symbols_parsed_and_uppercased():
    settings = Settings(_env_file=None, watchlist=" reliance, tcs ,infy ")
    assert settings.watchlist_symbols == ["RELIANCE", "TCS", "INFY"]


def test_is_live_mode_reflects_execution_mode():
    live = Settings(_env_file=None, vibetrading_execution_mode="live")
    paper = Settings(_env_file=None, vibetrading_execution_mode="paper")
    assert live.is_live_mode is True
    assert paper.is_live_mode is False
