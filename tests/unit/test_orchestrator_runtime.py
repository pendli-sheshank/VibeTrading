from __future__ import annotations

from vibetrading.orchestrator.runtime import should_restart


def test_should_restart_false_for_empty_change_set():
    assert should_restart(set()) is False


def test_should_restart_false_when_only_risk_limit_keys_changed():
    assert should_restart({"risk_max_daily_loss_inr", "risk_max_position_size_inr"}) is False


def test_should_restart_true_for_a_single_non_risk_limit_key():
    assert should_restart({"dhan_access_token"}) is True


def test_should_restart_true_when_change_set_mixes_risk_limits_with_anything_else():
    assert should_restart({"risk_max_daily_loss_inr", "vibetrading_execution_mode"}) is True
