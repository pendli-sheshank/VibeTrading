from __future__ import annotations

import json
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from vibetrading.config import Settings, get_settings
from vibetrading.persistence.repositories import (
    delete_app_setting,
    list_app_settings,
    upsert_app_setting,
)
from vibetrading.settings.crypto import decrypt_value, encrypt_value
from vibetrading.settings.registry import SETTINGS_REGISTRY


async def load_settings_from_db(session: AsyncSession, settings: Settings | None = None) -> Settings:
    """Fetches every app_settings row, decrypts secrets, and applies them on
    top of the given (default: process singleton) Settings object.

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
    settings = settings or get_settings()

    base = settings.model_dump()
    for key, field in SETTINGS_REGISTRY.items():
        base[key] = field.default

    overrides: dict[str, Any] = {}
    for row in await list_app_settings(session):
        field = SETTINGS_REGISTRY.get(row.key)
        if field is None or row.value is None:
            continue
        raw = decrypt_value(row.value) if row.is_secret else row.value
        overrides[row.key] = json.loads(raw)

    merged = Settings.model_validate({**base, **overrides})
    for name, value in merged.model_dump().items():
        setattr(settings, name, value)

    return settings


async def save_settings(session: AsyncSession, updates: dict[str, Any]) -> set[str]:
    """Persists `updates` (already-typed native Python values — e.g. a
    dashboard route is responsible for coercing form strings to the
    SettingField's declared type before calling this) and refreshes the
    live singleton. A blank ("" or None) value for a secret field means
    "leave unchanged" and is skipped, never written as an empty credential
    — use clear_secret() to actually remove one. Raises ValueError for any
    key not in SETTINGS_REGISTRY. Returns the set of keys actually written.
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
        await upsert_app_setting(session, key, stored_value, is_secret=field.secret)
        changed.add(key)

    if changed:
        await load_settings_from_db(session)

    return changed


async def clear_secret(session: AsyncSession, key: str) -> None:
    """Explicit credential-removal path: deletes the stored row (rather than
    writing a blank value) so the field reverts to its Settings class
    default on the next load."""
    field = SETTINGS_REGISTRY.get(key)
    if field is None or not field.secret:
        raise ValueError(f"{key!r} is not a clearable secret setting")

    await delete_app_setting(session, key)
    await load_settings_from_db(session)
