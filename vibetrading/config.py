from __future__ import annotations

from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict

from vibetrading.core.enums import ExecutionMode, KillSwitchMode


class Settings(BaseSettings):
    """Single source of truth for all environment configuration.

    See .env.example for the documented list of every variable this reads.
    """

    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    # --- Execution mode ---------------------------------------------------
    vibetrading_execution_mode: ExecutionMode = ExecutionMode.PAPER
    vibetrading_kill_switch: bool = False
    vibetrading_kill_switch_mode: KillSwitchMode = KillSwitchMode.HALT_NEW_ORDERS
    # Signs RiskApprovalTokens (risk/tokens.py). MUST be overridden with a
    # real random secret before running in live mode — the insecure default
    # is fine for dev/paper mode only.
    risk_token_secret: str = "dev-insecure-secret-change-me"

    # --- Dhan broker --------------------------------------------------------
    dhan_client_id: str = ""
    dhan_access_token: str = ""

    # --- LLM providers --------------------------------------------------------
    llm_default_provider: str = "anthropic"
    anthropic_api_key: str = ""
    openai_api_key: str = ""
    gemini_api_key: str = ""
    openrouter_api_key: str = ""
    llm_model_research: str = ""
    llm_model_strategy: str = ""
    llm_model_backtest: str = ""

    # --- News / social sources -------------------------------------------------
    news_api_key: str = ""
    news_source_enabled: bool = False

    chat_source_twitter_enabled: bool = False
    twitter_bearer_token: str = ""

    chat_source_reddit_enabled: bool = False
    reddit_client_id: str = ""
    reddit_client_secret: str = ""

    chat_source_telegram_enabled: bool = False
    telegram_api_id: str = ""
    telegram_api_hash: str = ""

    chat_source_stocktwits_enabled: bool = False
    chat_source_valuepickr_enabled: bool = False

    # --- Database --------------------------------------------------------
    database_url: str = "sqlite+aiosqlite:///./vibetrading.db"
    database_pool_size: int = 10
    database_max_overflow: int = 20

    # --- Cross-worker realtime fan-out (Redis) -----------------------------
    # None (the default) means "in-process fan-out only" -- every existing
    # dev/test path and the single-process deployment model both work with
    # zero Redis dependency. Set only when running >1 API/worker replica,
    # so a WebSocket connected to one replica sees events published by a
    # scheduler running in another.
    redis_url: str | None = None

    # --- Alerting -----------------------------------------------------------
    # None (the default) means alerts are logged only, never sent anywhere
    # -- every dev/test path needs zero external dependency. Set to a
    # Slack incoming-webhook URL (or any endpoint accepting {"text": ...})
    # to also push a message there when a circuit breaker opens.
    alert_webhook_url: str | None = None

    # --- Scheduling intervals (seconds) -----------------------------------
    enable_scheduler: bool = True
    agent_interval_research_sec: int = 900
    agent_interval_strategy_sec: int = 300
    agent_interval_stop_loss_monitor_sec: int = 30

    # --- Risk Agent limits --------------------------------------------------
    risk_max_position_size_inr: float = 50_000
    risk_max_pct_capital_per_stock: float = 0.10
    risk_max_concurrent_positions: int = 5
    risk_max_daily_loss_inr: float = 10_000
    risk_mandatory_stop_loss_pct: float = 0.03
    risk_min_signal_confidence: float = 0.65
    risk_max_total_exposure_pct: float = 0.50

    # --- Auth -----------------------------------------------------------------
    # Signs session cookies (JWT) minted on login. MUST be overridden with a
    # real random secret before running with real user accounts — same
    # insecure-default convention as risk_token_secret/app_secrets_key.
    auth_secret_key: str = "dev-insecure-auth-secret-change-me"
    # Cookies are only ever sent over HTTPS when true. Defaults off so local
    # http://localhost dev works out of the box; MUST be true in production.
    auth_cookie_secure: bool = False

    # --- App server -----------------------------------------------------------
    host: str = "0.0.0.0"
    port: int = 8000
    log_level: str = "INFO"

    # Encrypts secret values stored in the app_settings table (Dhan token,
    # LLM keys, etc — see vibetrading/settings/crypto.py). Stays env-only
    # deliberately: you need this key to decrypt anything in the DB, so it
    # can't itself live in the DB. MUST be overridden before real use, same
    # as risk_token_secret — rotating it orphans previously-stored secrets.
    app_secrets_key: str = "dev-insecure-key-change-me"

    @property
    def has_dhan_credentials(self) -> bool:
        return bool(self.dhan_client_id and self.dhan_access_token)

    @property
    def is_live_mode(self) -> bool:
        return self.vibetrading_execution_mode == ExecutionMode.LIVE

    def llm_key_for(self, provider: str) -> str:
        return {
            "anthropic": self.anthropic_api_key,
            "openai": self.openai_api_key,
            "gemini": self.gemini_api_key,
            "openrouter": self.openrouter_api_key,
        }.get(provider, "")


@lru_cache
def get_settings() -> Settings:
    return Settings()
