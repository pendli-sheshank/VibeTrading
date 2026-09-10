from __future__ import annotations

from vibetrading.persistence.repositories import (
    delete_app_setting,
    get_app_setting,
    list_app_settings,
    upsert_app_setting,
)

TENANT_ID = 1


async def test_upsert_and_get_app_setting(db_session):
    await upsert_app_setting(db_session, TENANT_ID, "llm_default_provider", '"anthropic"', is_secret=False)
    await db_session.flush()

    row = await get_app_setting(db_session, TENANT_ID, "llm_default_provider")
    assert row is not None
    assert row.value == '"anthropic"'
    assert row.is_secret is False


async def test_upsert_is_idempotent_and_updates_in_place(db_session):
    await upsert_app_setting(db_session, TENANT_ID, "dhan_client_id", '"old"', is_secret=False)
    await db_session.flush()
    await upsert_app_setting(db_session, TENANT_ID, "dhan_client_id", '"new"', is_secret=False)
    await db_session.flush()

    rows = await list_app_settings(db_session, TENANT_ID)
    assert len(rows) == 1
    assert rows[0].value == '"new"'


async def test_delete_app_setting_removes_row(db_session):
    await upsert_app_setting(db_session, TENANT_ID, "dhan_access_token", "ciphertext", is_secret=True)
    await db_session.flush()

    await delete_app_setting(db_session, TENANT_ID, "dhan_access_token")
    await db_session.flush()

    assert await get_app_setting(db_session, TENANT_ID, "dhan_access_token") is None


async def test_delete_missing_key_is_a_no_op(db_session):
    await delete_app_setting(db_session, TENANT_ID, "does_not_exist")  # should not raise


async def test_list_app_settings_returns_all_rows(db_session):
    await upsert_app_setting(db_session, TENANT_ID, "risk_max_daily_loss_inr", "5000.0", is_secret=False)
    await upsert_app_setting(db_session, TENANT_ID, "news_api_key", "ciphertext", is_secret=True)
    await db_session.flush()

    rows = await list_app_settings(db_session, TENANT_ID)
    assert {r.key for r in rows} == {"risk_max_daily_loss_inr", "news_api_key"}


async def test_settings_are_isolated_per_tenant(db_session):
    await upsert_app_setting(db_session, 1, "dhan_client_id", '"tenant-1"', is_secret=False)
    await upsert_app_setting(db_session, 2, "dhan_client_id", '"tenant-2"', is_secret=False)
    await db_session.flush()

    row_1 = await get_app_setting(db_session, 1, "dhan_client_id")
    row_2 = await get_app_setting(db_session, 2, "dhan_client_id")
    assert row_1.value == '"tenant-1"'
    assert row_2.value == '"tenant-2"'
    assert {r.key for r in await list_app_settings(db_session, 1)} == {"dhan_client_id"}
