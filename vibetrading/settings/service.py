from __future__ import annotations

import json
import logging
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from vibetrading.config import Settings
from vibetrading.persistence.repositories import (
    delete_app_setting,
    list_app_settings,
    upsert_app_setting,
)
from vibetrading.settings.cache import get_tenant_settings
from vibetrading.settings.crypto import SecretCryptoError, decrypt_value, encrypt_value
from vibetrading.settings.registry import SETTINGS_REGISTRY

logger = logging.getLogger(__name__)


async def load_settings_from_db(session: AsyncSession, tenant_id: int, settings: Settings | None = None) -> Settings:
    """Fetches every app_settings row for this tenant, decrypts secrets, and
    applies them on top of the given (default: this tenant's cached object,
    see settings/cache.py) Settings object.

    Mutates `settings` IN PLACE (setattr per field) rather than returning a
    new object, so every already-constructed holder of this exact reference
    (RiskEngine._settings, etc.) sees the update automatically without any
    code changes at those call sites. A fully validated Settings instance is
    built first via model_validate() so every field gets correct type
    coercion/enum parsing in one place before any attribute is set.

    Every registry-tracked field is reset to its class default before DB
    rows are applied (not merged onto whatever's currently on `settings`) —
    otherwise a deleted row (see clear_secret()) would silently keep
    whatever value was mutated in by an earlier load rather than reverting.
    Non-registry fields (database_url, host, ... — env-only, never written
    here) are left exactly as they are on the current object.
    """
    settings = settings or get_tenant_settings(tenant_id)

    base = settings.model_dump()
    for key, field in SETTINGS_REGISTRY.items():
        base[key] = field.default

    overrides: dict[str, Any] = {}
    for row in await list_app_settings(session, tenant_id):
        field = SETTINGS_REGISTRY.get(row.key)
        if field is None or row.value is None:
            continue
        try:
            raw = decrypt_value(row.value) if row.is_secret else row.value
            overrides[row.key] = json.loads(raw)
        except SecretCryptoError:
            # APP_SECRETS_KEY changed since this row was written -- skip it
            # (the field keeps its already-reset class default above)
            # rather than crashing the whole app on startup over one
            # undecryptable credential.
            logger.warning(
                "Could not decrypt stored setting %r for tenant_id=%s; falling back to its default.",
                row.key,
                tenant_id,
            )

    merged = Settings.model_validate({**base, **overrides})
    for name, value in merged.model_dump().items():
        setattr(settings, name, value)

    return settings


async def save_settings(session: AsyncSession, tenant_id: int, updates: dict[str, Any]) -> set[str]:
    """Persists `updates` (already-typed native Python values — e.g. a
    dashboard route is responsible for coercing form strings to the
    SettingField's declared type before calling this) for this tenant and
    refreshes their live cached Settings object. A blank ("" or None) value
    for a secret field means "leave unchanged" and is skipped, never
    written as an empty credential — use clear_secret() to actually remove
    one. Raises ValueError for any key not in SETTINGS_REGISTRY. Returns
    the set of keys actually written.
    """
    changed: set[str] = set()

    for key, value in updates.items():
        field = SETTINGS_REGISTRY.get(key)
        if field is None:
            raise ValueError(f"Unknown setting key: {key!r}")

        if field.secret and (value is None or value == ""):
            continue

        raw = json.dumps(value)
        stored_value = encrypt_value(raw) if field.secret else raw
        await upsert_app_setting(session, tenant_id, key, stored_value, is_secret=field.secret)
        changed.add(key)

    if changed:
        await load_settings_from_db(session, tenant_id)

    return changed


async def clear_secret(session: AsyncSession, tenant_id: int, key: str) -> None:
    """Explicit credential-removal path: deletes the tenant's stored row
    (rather than writing a blank value) so the field reverts to its
    Settings class default on the next load."""
    field = SETTINGS_REGISTRY.get(key)
    if field is None or not field.secret:
        raise ValueError(f"{key!r} is not a clearable secret setting")

    await delete_app_setting(session, tenant_id, key)
    await load_settings_from_db(session, tenant_id)
