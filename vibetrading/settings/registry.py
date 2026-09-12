from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from vibetrading.config import Settings
from vibetrading.core.enums import ExecutionMode

SECTIONS: tuple[str, ...] = ("execution_broker", "llm")

# Fields intentionally NOT in the registry (never DB-backed via this system):
#   database_url, host, port, log_level, app_secrets_key, auth_secret_key,
#     auth_cookie_secure — infra, needed before DB access (or any tenant)
#     exists.
#   risk_token_secret — platform-wide, not per-tenant: it only proves an
#     order's RiskApprovalToken was minted by THIS platform's RiskEngine
#     (see risk/tokens.py), not a tenant secret, so there's no reason to
#     pay the multi-tenant-settings complexity tax on it. Env-only, same as
#     app_secrets_key.
#   risk_max_position_size_inr, risk_max_pct_capital_per_stock,
#     risk_max_concurrent_positions, risk_max_daily_loss_inr,
#     risk_mandatory_stop_loss_pct, risk_min_signal_confidence,
#     risk_max_total_exposure_pct — deliberately env-only, not per-tenant
#     UI-editable: keeps the Settings page to the two things that matter
#     day-to-day (broker + LLM). RiskConfig.from_settings() still reads
#     them straight off the Settings object, so overriding one is a normal
#     .env edit, just not a per-account one.
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

SETTINGS_REGISTRY: dict[str, SettingField] = {f.key: f for f in [*_EXECUTION_BROKER_FIELDS, *_LLM_FIELDS]}


def fields_in_section(section: str) -> list[SettingField]:
    return [f for f in SETTINGS_REGISTRY.values() if f.section == section]
