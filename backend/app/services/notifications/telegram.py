"""Telegram push notifications via a bot (Bot API) -- optional, a no-op
unless TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID are set in the environment
(see Settings). A bot can't message a phone number directly; the chat has
to be started from that side first:

  1. In Telegram, message @BotFather, send /newbot, follow the prompts --
     it replies with a token. Set TELEGRAM_BOT_TOKEN to that.
  2. From the phone/account that should receive alerts, open the new bot
     and send it any message (e.g. /start).
  3. GET https://api.telegram.org/bot<token>/getUpdates and read the
     numeric "chat":{"id": ...} out of the response -- set
     TELEGRAM_CHAT_ID to that (never the phone number itself, which is
     never transmitted to Telegram by this module).

Ported from the standalone FLY OI SCN scanner's notifiers.py, made async
to match this backend's httpx convention.
"""

import html
import logging

import httpx

from app.core.config import get_settings

logger = logging.getLogger(__name__)

# Telegram's real per-message cap is 4096 chars; this leaves headroom for
# the <b>/<pre> wrapper tags added below.
TELEGRAM_MAX_LEN = 4000


async def send_telegram(subject: str, body: str) -> None:
    """Never raises: a Telegram timeout/connection error used to propagate
    straight out of the calling strategy's evaluate(), aborting that tick
    part-way -- e.g. after the 9:20 scan had already been marked done but
    before the remaining per-stock alerts went out, which then never got
    sent at all. A failed push is logged and skipped instead; the in-app
    alert the caller creates alongside it is unaffected."""
    settings = get_settings()
    if not settings.telegram_bot_token or not settings.telegram_chat_id:
        return

    api_url = f"https://api.telegram.org/bot{settings.telegram_bot_token}/sendMessage"
    full_text = f"<b>{html.escape(subject)}</b>\n<pre>{html.escape(body)}</pre>"
    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            for chunk in _chunk(full_text):
                resp = await client.post(api_url, json={"chat_id": settings.telegram_chat_id, "text": chunk, "parse_mode": "HTML"})
                if resp.status_code != 200:
                    logger.error("Telegram send failed (%s): %s", resp.status_code, resp.text)
    except httpx.HTTPError as exc:
        logger.error("Telegram send failed for %r: %s: %s", subject, type(exc).__name__, exc)


def _chunk(text: str, limit: int = TELEGRAM_MAX_LEN) -> list[str]:
    if len(text) <= limit:
        return [text]
    # Split on line breaks so a <pre> block's monospace table isn't cut mid-row.
    lines = text.split("\n")
    chunks: list[str] = []
    current = ""
    for line in lines:
        candidate = current + ("\n" if current else "") + line
        if len(candidate) > limit:
            if current:
                chunks.append(current)
            current = line
        else:
            current = candidate
    if current:
        chunks.append(current)
    return chunks
