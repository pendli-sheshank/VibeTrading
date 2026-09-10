from __future__ import annotations

import httpx

from vibetrading.config import get_settings
from vibetrading.observability.alerts import send_alert


async def test_send_alert_is_a_no_op_beyond_logging_without_a_webhook_configured(monkeypatch, caplog):
    monkeypatch.setattr(get_settings(), "alert_webhook_url", None)
    with caplog.at_level("WARNING"):
        await send_alert("something broke")  # must not raise
    assert "something broke" in caplog.text


async def test_send_alert_posts_to_the_configured_webhook(monkeypatch):
    monkeypatch.setattr(get_settings(), "alert_webhook_url", "https://hooks.example.com/webhook")

    calls = []

    class FakeResponse:
        def raise_for_status(self):
            pass

    class FakeAsyncClient:
        def __init__(self, *args, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc_info):
            return False

        async def post(self, url, json):
            calls.append((url, json))
            return FakeResponse()

    monkeypatch.setattr(httpx, "AsyncClient", FakeAsyncClient)

    await send_alert("circuit breaker opened")

    assert calls == [("https://hooks.example.com/webhook", {"text": "circuit breaker opened"})]


async def test_send_alert_swallows_a_webhook_delivery_failure(monkeypatch):
    monkeypatch.setattr(get_settings(), "alert_webhook_url", "https://hooks.example.com/webhook")

    class FailingAsyncClient:
        def __init__(self, *args, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc_info):
            return False

        async def post(self, url, json):
            raise httpx.ConnectError("connection refused")

    monkeypatch.setattr(httpx, "AsyncClient", FailingAsyncClient)

    await send_alert("circuit breaker opened")  # must not raise
