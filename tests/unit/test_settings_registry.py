from __future__ import annotations

from vibetrading.config import Settings
from vibetrading.risk.config import RiskConfig
from vibetrading.settings.registry import SECTIONS, SETTINGS_REGISTRY, fields_in_section

EXCLUDED_FIELDS = {
    "database_url",
    "database_pool_size",
    "database_max_overflow",
    "redis_url",
    "alert_webhook_url",
    "worker_role",
    "host",
    "port",
    "log_level",
    "app_secrets_key",
    "auth_secret_key",
    "auth_cookie_secure",
    "risk_token_secret",
    "watchlist",
    "vibetrading_kill_switch",
    "vibetrading_kill_switch_mode",
    # Env-only: not per-tenant Settings-UI-editable (see registry.py) --
    # RiskConfig.from_settings() reads these straight off Settings, not
    # through the registry.
    "risk_max_position_size_inr",
    "risk_max_pct_capital_per_stock",
    "risk_max_concurrent_positions",
    "risk_max_daily_loss_inr",
    "risk_mandatory_stop_loss_pct",
    "risk_min_signal_confidence",
    "risk_max_total_exposure_pct",
}


def test_every_non_excluded_settings_field_is_registered_exactly_once():
    all_fields = set(Settings.model_fields.keys())
    expected = all_fields - EXCLUDED_FIELDS
    assert set(SETTINGS_REGISTRY.keys()) == expected


def test_every_entry_has_a_valid_section():
    for entry in SETTINGS_REGISTRY.values():
        assert entry.section in SECTIONS


def test_fields_in_section_matches_registry_filter():
    for section in SECTIONS:
        expected = {k for k, f in SETTINGS_REGISTRY.items() if f.section == section}
        actual = {f.key for f in fields_in_section(section)}
        assert actual == expected


def test_risk_limit_fields_are_not_registered_but_still_feed_risk_config():
    """risk_* fields are deliberately env-only (see registry.py) -- not in
    SETTINGS_REGISTRY at all, so not per-tenant Settings-UI-editable -- but
    RiskConfig.from_settings() must keep reading every one of them straight
    off the Settings object regardless.
    """
    risk_keys = {k for k in Settings.model_fields if k.startswith("risk_") and k != "risk_token_secret"}
    assert risk_keys.isdisjoint(SETTINGS_REGISTRY.keys())

    dummy_settings = Settings(_env_file=None)
    config = RiskConfig.from_settings(dummy_settings)
    for key in risk_keys:
        risk_config_attr = key.removeprefix("risk_")
        assert hasattr(config, risk_config_attr), f"{key} has no matching RiskConfig field"


def test_default_matches_settings_class_default():
    defaults = Settings(_env_file=None)
    for key, entry in SETTINGS_REGISTRY.items():
        assert entry.default == getattr(defaults, key), f"{key} default mismatch"


def test_secret_fields_are_marked():
    expected_secrets = {
        "dhan_access_token",
        "anthropic_api_key",
        "openai_api_key",
        "gemini_api_key",
        "openrouter_api_key",
    }
    actual_secrets = {k for k, f in SETTINGS_REGISTRY.items() if f.secret}
    assert actual_secrets == expected_secrets
