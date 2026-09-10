from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from vibetrading.config import Settings
from vibetrading.core.enums import ExecutionMode

SECTIONS: tuple[str, ...] = ("execution_broker", "llm", "risk_limits", "data_sources")

# Fields intentionally NOT in the registry (never DB-backed via this system):
#   database_url, host, port, log_level, app_secrets_key, auth_secret_key,
#     auth_cookie_secure — infra, needed before DB access (or any tenant)
#     exists.
#   risk_token_secret — platform-wide, not per-tenant: it only proves an
#     order's RiskApprovalToken was minted by THIS platform's RiskEngine
#     (see risk/tokens.py), not a tenant secret, so there's no reason to
#     pay the multi-tenant-settings complexity tax on it. Env-only, same as
#     app_secrets_key.
#   watchlist — superseded by the `stocks` table (see orchestrator/watchlist.py).
#   vibetrading_kill_switch, vibetrading_kill_switch_mode — already have a
#     live, authoritative home in RiskStateORM (edited via /risk/kill-switch);
#     duplicating them here would create two sources of truth. RiskConfig
#     .from_settings() reads vibetrading_kill_switch_mode too, but
#     risk/rules.py's KillSwitchRule actually checks ctx.risk_state
#     .kill_switch_mode (RiskStateORM), never ctx.config.kill_switch_mode —
#     so that Settings field only ever seeds RiskStateORM's initial value.


@dataclass(frozen=True)
class SettingField:
    key: str
    section: str
    type: type
    default: Any
    secret: bool = False
    label: str = ""
    help_text: str = ""
    choices: list[str] | None = None


def _default_for(key: str) -> Any:
    return Settings.model_fields[key].default


def _field(
    key: str,
    section: str,
    type_: type,
    *,
    secret: bool = False,
    label: str = "",
    help_text: str = "",
    choices: list[str] | None = None,
) -> SettingField:
    return SettingField(
        key=key,
        section=section,
        type=type_,
        default=_default_for(key),
        secret=secret,
        label=label or key.replace("_", " ").title(),
        help_text=help_text,
        choices=choices,
    )


_EXECUTION_BROKER_FIELDS = [
    _field(
        "vibetrading_execution_mode",
        "execution_broker",
        str,
        label="Execution mode",
        choices=[m.value for m in ExecutionMode],
        help_text="Live places real orders with no per-trade approval. Switching to live requires explicit confirmation.",
    ),
    _field("dhan_client_id", "execution_broker", str, label="Dhan client ID"),
    _field("dhan_access_token", "execution_broker", str, secret=True, label="Dhan access token"),
    _field("enable_scheduler", "execution_broker", bool, label="Run the orchestrator scheduler"),
    _field("agent_interval_research_sec", "execution_broker", int, label="Research agent interval (seconds)"),
    _field("agent_interval_strategy_sec", "execution_broker", int, label="Strategy agent interval (seconds)"),
    _field(
        "agent_interval_stop_loss_monitor_sec", "execution_broker", int, label="Stop-loss monitor interval (seconds)"
    ),
]

_LLM_FIELDS = [
    _field(
        "llm_default_provider",
        "llm",
        str,
        label="LLM provider",
        choices=["anthropic", "openai", "gemini", "openrouter"],
    ),
    _field("anthropic_api_key", "llm", str, secret=True, label="Anthropic API key"),
    _field("openai_api_key", "llm", str, secret=True, label="OpenAI API key"),
    _field("gemini_api_key", "llm", str, secret=True, label="Gemini API key"),
    _field("openrouter_api_key", "llm", str, secret=True, label="OpenRouter API key"),
    _field("llm_model_research", "llm", str, label="Research agent model override"),
    _field("llm_model_strategy", "llm", str, label="Strategy agent model override"),
    _field("llm_model_backtest", "llm", str, label="Backtest agent model override"),
]

# Every one of these must stay exactly the set of `risk_*` fields
# risk/config.py's RiskConfig.from_settings() reads — see
# tests/unit/test_settings_registry.py, which asserts this equality so the
# runtime-restart exemption for this section (orchestrator/runtime.py) can
# never silently go stale.
_RISK_LIMITS_FIELDS = [
    _field("risk_max_position_size_inr", "risk_limits", float, label="Max position size (INR)"),
    _field("risk_max_pct_capital_per_stock", "risk_limits", float, label="Max % capital per stock"),
    _field("risk_max_concurrent_positions", "risk_limits", int, label="Max concurrent positions"),
    _field("risk_max_daily_loss_inr", "risk_limits", float, label="Max daily loss (INR)"),
    _field("risk_mandatory_stop_loss_pct", "risk_limits", float, label="Mandatory stop-loss %"),
    _field("risk_min_signal_confidence", "risk_limits", float, label="Min signal confidence"),
    _field("risk_max_total_exposure_pct", "risk_limits", float, label="Max total exposure %"),
]

_DATA_SOURCE_FIELDS = [
    _field("news_api_key", "data_sources", str, secret=True, label="NewsAPI key"),
    _field("news_source_enabled", "data_sources", bool, label="Enable NewsAPI source"),
    _field("chat_source_twitter_enabled", "data_sources", bool, label="Enable Twitter source"),
    _field("twitter_bearer_token", "data_sources", str, secret=True, label="Twitter bearer token"),
    _field("chat_source_reddit_enabled", "data_sources", bool, label="Enable Reddit source"),
    _field("reddit_client_id", "data_sources", str, secret=True, label="Reddit client ID"),
    _field("reddit_client_secret", "data_sources", str, secret=True, label="Reddit client secret"),
    _field("chat_source_telegram_enabled", "data_sources", bool, label="Enable Telegram source"),
    _field("telegram_api_id", "data_sources", str, secret=True, label="Telegram API ID"),
    _field("telegram_api_hash", "data_sources", str, secret=True, label="Telegram API hash"),
    _field("chat_source_stocktwits_enabled", "data_sources", bool, label="Enable StockTwits source"),
    _field("chat_source_valuepickr_enabled", "data_sources", bool, label="Enable ValuePickr source"),
]

SETTINGS_REGISTRY: dict[str, SettingField] = {
    f.key: f
    for f in [*_EXECUTION_BROKER_FIELDS, *_LLM_FIELDS, *_RISK_LIMITS_FIELDS, *_DATA_SOURCE_FIELDS]
}


def fields_in_section(section: str) -> list[SettingField]:
    return [f for f in SETTINGS_REGISTRY.values() if f.section == section]
