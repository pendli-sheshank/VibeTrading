from __future__ import annotations

from vibetrading.orchestrator.runtime import should_restart


def test_should_restart_false_for_empty_change_set():
    assert should_restart(set()) is False


def test_should_restart_true_for_a_single_changed_key():
    assert should_restart({"dhan_access_token"}) is True


def test_should_restart_true_for_multiple_changed_keys():
    assert should_restart({"llm_default_provider", "vibetrading_execution_mode"}) is True
