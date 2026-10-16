"""Telegram alerts copied to a group as well as the private chat (15 Oct).

Which alerts: NIFTY's OI levels (services/options/oi_levels.py) and the F&O
Opening Momentum Scanner's (send_telegram's to_group, or the
telegram_to_group context the native runner sets for that strategy -- see
sends_to_group). Everything else -- strategy trades, the 08:45 Zerodha
login reminder -- stays in the private chat only.

Which group: a bot learns a group's ID only from its own updates -- when it
is added (my_chat_member) and, as an admin, from every message there; it
can't look a group up by its name. So each lookup reads the bot's pending
updates (getUpdates, kept by Telegram for 24 hours), records the groups seen
in telegram_chats, and picks the one whose name is
Settings.telegram_group_name -- or, unset, the group named on 15 Oct, kept
here only as a hash since this repository is public. It must also have the
private chat's owner as a member, so a stranger's group of the same name
never receives the alerts. Found, it is reused for RECHECK_AFTER.

Telegram upgrading the group to a supergroup gives it a new ID: the send
error names it, and the alert is sent again there. The bot removed from the
group: marked left, and the alerts stay private until it is back. A group
not found yet (added over a day ago with no message since): any message
posted in the group lets the bot see it.
"""

import hashlib
import json
import logging
from contextvars import ContextVar
from datetime import datetime, timedelta, timezone

import httpx
from sqlalchemy import select

from app.core.config import get_settings
from app.db.session import AsyncSessionLocal
from app.models.telegram import TelegramChat

logger = logging.getLogger(__name__)

API = "https://api.telegram.org/bot{token}/{method}"
GROUP_TYPES = ("group", "supergroup")
# md5 of the group's name, lower-cased: "Open Interest Bot" (15 Oct).
DEFAULT_GROUP_NAME_MD5 = "ab6b5641909cbf3a378af555db210248"
# Native strategies whose alerts also go to the group, by their built-in code.
GROUP_STRATEGY_MARKERS = ("F&O Opening-Candle Momentum Scanner",)
MEMBER_STATUSES = ("creator", "administrator", "member", "restricted")
GONE_STATUSES = ("left", "kicked")
RECHECK_AFTER = timedelta(hours=6)
CAPTION_MAX = 1024  # a picture's caption, in characters
UPDATE_KINDS = ["message", "edited_message", "my_chat_member"]

# Set by the native runner while a strategy whose alerts go to the group runs.
telegram_to_group: ContextVar[bool] = ContextVar("telegram_to_group", default=False)

_offset: int | None = None  # the next update to ask for: the ones before are taken
_found: tuple[int, datetime] | None = None  # (group chat ID, when it was found)


def sends_to_group(python_code: str | None) -> bool:
    return any(marker in (python_code or "") for marker in GROUP_STRATEGY_MARKERS)


def name_matches(title: str | None, wanted: str | None) -> bool:
    name = (title or "").strip().lower()
    if wanted and wanted.strip():
        return name == wanted.strip().lower()
    return hashlib.md5(name.encode()).hexdigest() == DEFAULT_GROUP_NAME_MD5


def groups_in_updates(updates: list[dict]) -> tuple[dict[int, dict], list[tuple[int, int]]]:
    """({chat ID: {"type", "title", "bot_status"?}} for the groups in these
    updates, [(old ID, new ID)] for groups upgraded to a supergroup)."""
    groups: dict[int, dict] = {}
    moves: list[tuple[int, int]] = []
    for update in updates:
        member = update.get("my_chat_member")
        if member and member.get("chat", {}).get("type") in GROUP_TYPES:
            chat = member["chat"]
            groups.setdefault(chat["id"], {}).update(
                type=chat["type"], title=chat.get("title"), bot_status=member.get("new_chat_member", {}).get("status"),
            )
        for kind in ("message", "edited_message"):
            message = update.get(kind)
            if not message or message.get("chat", {}).get("type") not in GROUP_TYPES:
                continue
            chat = message["chat"]
            groups.setdefault(chat["id"], {}).update(type=chat["type"], title=chat.get("title"))
            if message.get("migrate_to_chat_id"):
                moves.append((chat["id"], message["migrate_to_chat_id"]))
            if message.get("migrate_from_chat_id"):
                moves.append((message["migrate_from_chat_id"], chat["id"]))
    return groups, moves


async def _record_move(db, old_id: int, new_id: int, now: datetime) -> None:
    old = await db.get(TelegramChat, old_id)
    new = await db.get(TelegramChat, new_id)
    if new is None:
        new = TelegramChat(chat_id=new_id, type="supergroup", title=old.title if old else None,
                           bot_status=old.bot_status if old else "member", seen_at=now)
        db.add(new)
    if old is not None:
        old.migrated_to = new_id


async def read_updates(db, client: httpx.AsyncClient, token: str) -> int:
    """Records the groups in the bot's pending updates; how many updates
    were read. Telegram refusing (another program reading this bot's
    updates, or a webhook set on it) is logged and reads none."""
    global _offset
    params = {"timeout": 0, "allowed_updates": json.dumps(UPDATE_KINDS)}
    if _offset is not None:
        params["offset"] = _offset
    resp = await client.get(API.format(token=token, method="getUpdates"), params=params)
    data = resp.json() if resp.headers.get("content-type", "").startswith("application/json") else {}
    if not data.get("ok"):
        logger.warning("Telegram getUpdates refused (%s): %s", resp.status_code, data.get("description"))
        return 0
    updates = data.get("result") or []
    if updates:
        _offset = max(u["update_id"] for u in updates) + 1
    groups, moves = groups_in_updates(updates)
    now = datetime.now(timezone.utc)
    for chat_id, seen in groups.items():
        row = await db.get(TelegramChat, chat_id)
        if row is None:
            row = TelegramChat(chat_id=chat_id, type=seen["type"], bot_status="member", seen_at=now)
            db.add(row)
        row.type, row.title, row.seen_at = seen["type"], seen.get("title") or row.title, now
        if seen.get("bot_status"):
            row.bot_status = seen["bot_status"]
    for old_id, new_id in moves:
        await db.flush()
        await _record_move(db, old_id, new_id, now)
    await db.commit()
    return len(updates)


def _owner_id() -> int | None:
    """The private chat's ID is its owner's user ID (a positive number);
    None when it isn't a private chat, and the membership check is skipped."""
    try:
        owner = int(get_settings().telegram_chat_id or "")
    except ValueError:
        return None
    return owner if owner > 0 else None


async def _owner_is_member(client: httpx.AsyncClient, token: str, chat_id: int) -> bool:
    owner = _owner_id()
    if owner is None:
        return True
    resp = await client.get(API.format(token=token, method="getChatMember"), params={"chat_id": chat_id, "user_id": owner})
    data = resp.json() if resp.status_code == 200 else {}
    return data.get("ok") is True and data.get("result", {}).get("status") in MEMBER_STATUSES


async def find_group(client: httpx.AsyncClient, token: str) -> int | None:
    """The group's chat ID, or None while it isn't known."""
    global _found
    now = datetime.now(timezone.utc)
    if _found is not None and now - _found[1] < RECHECK_AFTER:
        return _found[0]
    _found = None
    async with AsyncSessionLocal() as db:
        await read_updates(db, client, token)
        rows = (
            await db.execute(
                select(TelegramChat)
                .where(TelegramChat.type.in_(GROUP_TYPES), TelegramChat.migrated_to.is_(None))
                .order_by(TelegramChat.seen_at.desc())
            )
        ).scalars().all()
    wanted = get_settings().telegram_group_name
    for row in rows:
        if row.bot_status in GONE_STATUSES or not name_matches(row.title, wanted):
            continue
        if await _owner_is_member(client, token, row.chat_id):
            _found = (row.chat_id, now)
            return row.chat_id
    return None


async def _mark(chat_id: int, *, moved_to: int | None = None, bot_status: str | None = None) -> None:
    async with AsyncSessionLocal() as db:
        if moved_to is not None:
            await _record_move(db, chat_id, moved_to, datetime.now(timezone.utc))
        row = await db.get(TelegramChat, chat_id)
        if row is not None and bot_status is not None:
            row.bot_status = bot_status
        await db.commit()


def _answer(resp: httpx.Response) -> dict:
    return resp.json() if resp.headers.get("content-type", "").startswith("application/json") else {}


async def post_message(client: httpx.AsyncClient, token: str, chat_id: int | str, text: str, photo: bytes | None = None) -> httpx.Response:
    """sendMessage -- or, with `photo` (PNG bytes), sendPhoto with `text` as
    its caption (CAPTION_MAX at most). A picture Telegram refuses (a 400 that
    isn't a group upgrade) goes again as the text alone."""
    if photo is not None:
        resp = await client.post(
            API.format(token=token, method="sendPhoto"), data={"chat_id": str(chat_id), "caption": text, "parse_mode": "HTML"},
            files={"photo": ("chart.png", photo, "image/png")},
        )
        if resp.status_code != 400 or (_answer(resp).get("parameters") or {}).get("migrate_to_chat_id"):
            return resp
        logger.warning("Telegram refused the picture (%s): %s -- sent as text", resp.status_code, _answer(resp).get("description"))
    return await client.post(API.format(token=token, method="sendMessage"), json={"chat_id": chat_id, "text": text, "parse_mode": "HTML"})


async def send_to_group(client: httpx.AsyncClient, token: str, chunks: list[str], photo: bytes | None = None) -> bool:
    """Sends these HTML chunks to the group, `photo` with the first (its
    caption). False when there's no group to send to (none found, or the
    bot was removed from it)."""
    global _found
    chat_id = await find_group(client, token)
    if chat_id is None:
        return False
    for n, chunk in enumerate(chunks):
        for _ in range(2):  # a second go only to an upgraded group's new ID
            resp = await post_message(client, token, chat_id, chunk, photo if n == 0 else None)
            if resp.status_code == 200:
                break
            data = _answer(resp)
            new_id = (data.get("parameters") or {}).get("migrate_to_chat_id")
            if new_id:
                await _mark(chat_id, moved_to=new_id)
                chat_id = new_id
                _found = (new_id, datetime.now(timezone.utc))
                continue
            logger.error("Telegram group send failed (%s): %s", resp.status_code, data.get("description"))
            if resp.status_code == 403 or "not found" in (data.get("description") or "").lower():
                await _mark(chat_id, bot_status="left")  # removed from the group: private only until it's back
                _found = None
                return False
            break
    return True


async def group_status() -> str:
    """For the health check: whether the group is found -- no name or ID."""
    settings = get_settings()
    if not settings.telegram_bot_token:
        return "telegram not set up"
    async with httpx.AsyncClient(timeout=15.0) as client:
        found = await find_group(client, settings.telegram_bot_token)
    async with AsyncSessionLocal() as db:
        groups = len((await db.execute(select(TelegramChat.chat_id).where(TelegramChat.type.in_(GROUP_TYPES)))).all())
    return f"{'found' if found is not None else 'not found'} ({groups} group(s) seen)"
