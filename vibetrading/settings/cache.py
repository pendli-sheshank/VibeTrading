from __future__ import annotations

from vibetrading.config import Settings

# One mutable Settings object per tenant, mirroring the old single
# @lru_cache'd get_settings() singleton's "mutate in place so every already-
# constructed holder sees the update" pattern (see settings/service.py) --
# just keyed by tenant now instead of being one process-wide object. Every
# tenant's copy independently re-reads the same env/.env-sourced infra
# defaults (database_url, host, app_secrets_key, ...); those fields are
# never tenant-specific and never DB-overridden, so having N identical
# copies of them is harmless.
_tenant_settings: dict[int, Settings] = {}


def get_tenant_settings(tenant_id: int) -> Settings:
    settings = _tenant_settings.get(tenant_id)
    if settings is None:
        settings = Settings()
        _tenant_settings[tenant_id] = settings
    return settings


def reset_tenant_settings_cache() -> None:
    """Test-only: clears the process-wide per-tenant cache so tests don't
    leak settings mutations into each other."""
    _tenant_settings.clear()
