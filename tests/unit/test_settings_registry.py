from __future__ import annotations

from vibetrading.config import Settings
from vibetrading.risk.config import RiskConfig
from vibetrading.settings.registry import SECTIONS, SETTINGS_REGISTRY, fields_in_section

EXCLUDED_FIELDS = {
    "database_url",
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


def test_risk_limits_section_matches_what_risk_config_reads():
    """RiskConfig.from_settings() reads 8 Settings fields, but
    vibetrading_kill_switch_mode is deliberately excluded from the registry
    (its authoritative live home is RiskStateORM, not app_settings — see
    registry.py's module docstring). The risk_limits section must be
    exactly the 7 risk_* fields, so orchestrator/runtime.py's
    restart-exemption for this section can never silently drift from what
    the Risk Agent actually reads.
    """
    risk_limit_keys = {f.key for f in fields_in_section("risk_limits")}
    expected = {k for k in Settings.model_fields if k.startswith("risk_") and k not in EXCLUDED_FIELDS}
    assert risk_limit_keys == expected

    # And every one of those keys is a real RiskConfig.from_settings() input.
    dummy_settings = Settings(_env_file=None)
    config = RiskConfig.from_settings(dummy_settings)
    for key in risk_limit_keys:
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
        "news_api_key",
        "twitter_bearer_token",
        "reddit_client_id",
        "reddit_client_secret",
        "telegram_api_id",
        "telegram_api_hash",
    }
    actual_secrets = {k for k, f in SETTINGS_REGISTRY.items() if f.secret}
    assert actual_secrets == expected_secrets
