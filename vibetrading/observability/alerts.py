from __future__ import annotations

import logging

import httpx

from vibetrading.config import get_settings

logger = logging.getLogger(__name__)


async def send_alert(message: str) -> None:
    """Always logs at WARNING (so a plain log-based alert rule still fires
    even with no webhook configured); also POSTs {"text": message} to
    settings().alert_webhook_url (Slack incoming-webhook shaped, but any
    endpoint accepting that body works) when one is set. Best-effort --
    a webhook failure is logged, never raised, so a broken alert channel
    can't itself take down whatever triggered the alert.
    """
    logger.warning("ALERT: %s", message)

    url = get_settings().alert_webhook_url
    if not url:
        return

    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            response = await client.post(url, json={"text": message})
            response.raise_for_status()
    except Exception:
        logger.warning("Failed to deliver alert to configured webhook.", exc_info=True)
