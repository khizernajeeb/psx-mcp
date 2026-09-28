"""Minimal Telegram Bot API notifier used to deliver alert events.

Reads TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID from the environment; never
hardcode these. If either is missing, sending is a no-op (logged), so alert
evaluation still runs and writes alert_events even without Telegram set up.
"""

from __future__ import annotations

import logging
import os

import httpx

log = logging.getLogger("psx_mcp.telegram")


async def send_message(text: str) -> bool:
    token = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
    chat_id = os.environ.get("TELEGRAM_CHAT_ID", "").strip()
    if not token or not chat_id:
        log.warning("TELEGRAM_BOT_TOKEN/TELEGRAM_CHAT_ID not set; skipping delivery")
        return False
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    try:
        async with httpx.AsyncClient(timeout=10.0) as c:
            r = await c.post(url, json={"chat_id": chat_id, "text": text})
        if r.status_code != 200:
            log.error("Telegram sendMessage failed: %s %s", r.status_code, r.text[:200])
            return False
        return True
    except httpx.HTTPError as e:
        log.error("Telegram sendMessage error: %r", e)
        return False
