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

import contextvars
import html
import logging

import httpx

from app.core.config import get_settings
from app.services.notifications.telegram_group import CAPTION_MAX, post_message, send_to_group, telegram_to_group

logger = logging.getLogger(__name__)

# The bot's chat is the administrator's own, so a strategy another user
# runs mustn't post to it: the native runner sets this for each evaluation
# from the strategy owner's role. Their in-app alerts are unaffected.
telegram_allowed: contextvars.ContextVar[bool] = contextvars.ContextVar("telegram_allowed", default=True)

# Telegram's real per-message cap is 4096 chars; this leaves headroom for
# the <b>/<pre> wrapper tags added below.
TELEGRAM_MAX_LEN = 4000


async def send_telegram(subject: str, body: str, *, to_group: bool = False, photo: bytes | None = None) -> None:
    """Never raises: a Telegram timeout/connection error used to propagate
    straight out of the calling strategy's evaluate(), aborting that tick
    part-way -- e.g. after the 9:20 scan had already been marked done but
    before the remaining per-stock alerts went out, which then never got
    sent at all. A failed push is logged and skipped instead; the in-app
    alert the caller creates alongside it is unaffected.

    `to_group` (or the telegram_to_group context): the group as well
    (telegram_group.py) -- after the private chat, and a group failure never
    costs the private message.

    `photo` (PNG bytes): sent as a picture with the message as its caption --
    or, too long for a caption, with the first line alone and the rest after
    it. Telegram refusing the picture still delivers the text."""
    settings = get_settings()
    if not settings.telegram_bot_token or not settings.telegram_chat_id or not telegram_allowed.get():
        return

    full_text = f"<b>{html.escape(subject)}</b>\n<pre>{html.escape(body)}</pre>"
    chunks = _chunk(full_text)
    if photo is not None and len(full_text) > CAPTION_MAX:
        chunks = [f"<b>{html.escape(subject)}</b>", *_chunk(f"<pre>{html.escape(body)}</pre>")]
    try:
        async with httpx.AsyncClient(timeout=30.0 if photo is not None else 15.0) as client:
            for n, chunk in enumerate(chunks):
                resp = await post_message(client, settings.telegram_bot_token, settings.telegram_chat_id, chunk, photo if n == 0 else None)
                if resp.status_code != 200:
                    logger.error("Telegram send failed (%s): %s", resp.status_code, resp.text)
    except httpx.HTTPError as exc:
        logger.error("Telegram send failed for %r: %s: %s", subject, type(exc).__name__, exc)
    if not (to_group or telegram_to_group.get()):
        return
    try:
        async with httpx.AsyncClient(timeout=30.0 if photo is not None else 15.0) as client:
            await send_to_group(client, settings.telegram_bot_token, chunks, photo)
    except Exception as exc:  # the group is extra: never let it break the caller
        logger.error("Telegram group send failed for %r: %s: %s", subject, type(exc).__name__, exc)


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
