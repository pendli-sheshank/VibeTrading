from __future__ import annotations

from datetime import UTC, datetime

import pytest

from vibetrading.broker.mock_client import MockBrokerClient
from vibetrading.config import get_settings
from vibetrading.core.enums import ActionType, SignalSource
from vibetrading.core.models import Signal, Stock
from vibetrading.risk.engine import RiskEngine
from vibetrading.settings.crypto import _fernet, decrypt_value
from vibetrading.settings.service import clear_secret, load_settings_from_db, save_settings

STOCK = Stock(symbol="TCS")


def make_signal(**overrides) -> Signal:
    defaults = dict(
        stock_symbol="TCS",
        timestamp=datetime.now(UTC),
        source=SignalSource.STRATEGY_AGENT,
        action=ActionType.BUY,
        confidence=0.9,
        reasoning="test",
        reference_price=100.0,
    )
    defaults.update(overrides)
    return Signal(**defaults)


async def test_risk_limit_change_applies_to_next_call_with_no_restart(db_session):
    """The core Phase 10 proof: RiskEngine captures `self._settings` once at
    construction (a known, pre-existing capture-once pattern in
    risk/engine.py), yet a risk-limit change made purely through
    save_settings() — no RiskEngine reconstruction, no restart of anything —
    is enforced on the very next approve_and_execute() call. This works
    because save_settings() mutates the same Settings singleton in place.
    """
    broker = MockBrokerClient(seed=1, initial_funds=1_000_000.0)
    engine = RiskEngine(broker=broker)  # no config= override -> reads live settings each call

    baseline = await engine.approve_and_execute(db_session, make_signal(), STOCK)
    assert baseline.approved is True
    assert baseline.risk_check.rule_results["max_position_size"] is True

    await save_settings(db_session, {"risk_max_position_size_inr": 1.0})
    await db_session.commit()

    tightened = await engine.approve_and_execute(db_session, make_signal(), STOCK)
    assert tightened.approved is False
    assert tightened.risk_check.rule_results["max_position_size"] is False


async def test_save_settings_rejects_unknown_key(db_session):
    with pytest.raises(ValueError, match="Unknown setting"):
        await save_settings(db_session, {"not_a_real_setting": 1})


async def test_save_settings_returns_changed_keys(db_session):
    changed = await save_settings(db_session, {"risk_max_daily_loss_inr": 2500.0, "enable_scheduler": False})
    assert changed == {"risk_max_daily_loss_inr", "enable_scheduler"}
    assert get_settings().risk_max_daily_loss_inr == 2500.0
    assert get_settings().enable_scheduler is False


async def test_secret_round_trips_through_encryption(db_session):
    await save_settings(db_session, {"dhan_access_token": "super-secret-token"})
    await db_session.commit()

    assert get_settings().dhan_access_token == "super-secret-token"

    from vibetrading.persistence.repositories import get_app_setting

    row = await get_app_setting(db_session, "dhan_access_token")
    assert row is not None
    assert row.is_secret is True
    assert row.value != "super-secret-token"
    assert decrypt_value(row.value) == '"super-secret-token"'


async def test_blank_secret_value_leaves_existing_credential_unchanged(db_session):
    await save_settings(db_session, {"dhan_access_token": "original-token"})
    await db_session.commit()

    changed = await save_settings(db_session, {"dhan_access_token": ""})
    assert changed == set()
    assert get_settings().dhan_access_token == "original-token"


async def test_clear_secret_reverts_to_class_default(db_session):
    await save_settings(db_session, {"dhan_access_token": "some-token"})
    await db_session.commit()
    assert get_settings().dhan_access_token == "some-token"

    await clear_secret(db_session, "dhan_access_token")
    await db_session.commit()

    assert get_settings().dhan_access_token == ""


async def test_clear_secret_rejects_non_secret_key(db_session):
    with pytest.raises(ValueError, match="not a clearable secret"):
        await clear_secret(db_session, "risk_max_daily_loss_inr")


async def test_load_settings_from_db_with_no_rows_is_a_no_op(db_session):
    before = get_settings().model_dump()
    result = await load_settings_from_db(db_session)
    assert result.model_dump() == before


async def test_undecryptable_secret_falls_back_to_default_instead_of_crashing(db_session, monkeypatch):
    """A rotated APP_SECRETS_KEY makes previously-stored secrets
    undecryptable. That must not crash the whole app on startup -- it
    should behave like the row was never there (class default), with only
    that one field affected."""
    await save_settings(db_session, {"dhan_access_token": "some-token"})
    await db_session.commit()
    assert get_settings().dhan_access_token == "some-token"

    monkeypatch.setattr(get_settings(), "app_secrets_key", "a-totally-different-key")
    _fernet.cache_clear()
    try:
        result = await load_settings_from_db(db_session)
    finally:
        _fernet.cache_clear()

    assert result.dhan_access_token == ""  # class default, not a crash


async def test_execution_mode_enum_round_trips_correctly(db_session):
    await save_settings(db_session, {"vibetrading_execution_mode": "live"})
    await db_session.commit()

    from vibetrading.core.enums import ExecutionMode

    assert get_settings().vibetrading_execution_mode == ExecutionMode.LIVE
    assert get_settings().is_live_mode is True
