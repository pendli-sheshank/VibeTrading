from __future__ import annotations

from vibetrading.persistence.repositories import (
    delete_app_setting,
    get_app_setting,
    list_app_settings,
    upsert_app_setting,
)


async def test_upsert_and_get_app_setting(db_session):
    await upsert_app_setting(db_session, "llm_default_provider", '"anthropic"', is_secret=False)
    await db_session.flush()

    row = await get_app_setting(db_session, "llm_default_provider")
    assert row is not None
    assert row.value == '"anthropic"'
    assert row.is_secret is False


async def test_upsert_is_idempotent_and_updates_in_place(db_session):
    await upsert_app_setting(db_session, "dhan_client_id", '"old"', is_secret=False)
    await db_session.flush()
    await upsert_app_setting(db_session, "dhan_client_id", '"new"', is_secret=False)
    await db_session.flush()

    rows = await list_app_settings(db_session)
    assert len(rows) == 1
    assert rows[0].value == '"new"'


async def test_delete_app_setting_removes_row(db_session):
    await upsert_app_setting(db_session, "dhan_access_token", "ciphertext", is_secret=True)
    await db_session.flush()

    await delete_app_setting(db_session, "dhan_access_token")
    await db_session.flush()

    assert await get_app_setting(db_session, "dhan_access_token") is None


async def test_delete_missing_key_is_a_no_op(db_session):
    await delete_app_setting(db_session, "does_not_exist")  # should not raise


async def test_list_app_settings_returns_all_rows(db_session):
    await upsert_app_setting(db_session, "risk_max_daily_loss_inr", "5000.0", is_secret=False)
    await upsert_app_setting(db_session, "news_api_key", "ciphertext", is_secret=True)
    await db_session.flush()

    rows = await list_app_settings(db_session)
    assert {r.key for r in rows} == {"risk_max_daily_loss_inr", "news_api_key"}
