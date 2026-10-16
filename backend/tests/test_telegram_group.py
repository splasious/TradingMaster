"""Telegram alerts copied to a group (services/notifications/telegram_group.py,
15 Oct): the OI-levels and F&O scanner alerts go to the private chat and to
the group named "Open Interest Bot" -- found from the bot's updates, and
only with the private chat's owner in it."""

import json
import re

import httpx
import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.core.config import get_settings
from app.models.telegram import TelegramChat
from app.services.notifications import telegram, telegram_group
from app.services.notifications.telegram import send_telegram
from app.services.notifications.telegram_group import groups_in_updates, name_matches, sends_to_group, telegram_to_group

OWNER = 555
GROUP, OTHER_GROUP, SUPERGROUP = -100123, -100999, -100777
RealClient = httpx.AsyncClient


def added(update_id: int, chat_id: int, title: str, status: str = "administrator") -> dict:
    return {"update_id": update_id, "my_chat_member": {"chat": {"id": chat_id, "type": "supergroup", "title": title},
                                                      "new_chat_member": {"status": status}}}


def posted(update_id: int, chat_id: int, title: str, **extra) -> dict:
    return {"update_id": update_id, "message": {"chat": {"id": chat_id, "type": "group", "title": title}, "text": "hi", **extra}}


class FakeTelegram:
    """The Bot API calls the app makes: getUpdates, getChatMember, sendMessage."""

    def __init__(self):
        self.updates: list[dict] = []
        self.members: dict[tuple[int, int], str] = {}
        self.moved: dict[int, int] = {}  # group upgraded: old ID -> new ID
        self.removed: set[int] = set()
        self.sent: list[int] = []
        self.offsets: list[int | None] = []
        self.calls: list[tuple[str, int, str]] = []  # (method, chat ID, text or caption)
        self.refuse_photos = False

    def handle(self, request: httpx.Request) -> httpx.Response:
        method = request.url.path.rsplit("/", 1)[-1]
        if method == "getUpdates":
            offset = request.url.params.get("offset")
            self.offsets.append(int(offset) if offset else None)
            pending = [u for u in self.updates if offset is None or u["update_id"] >= int(offset)]
            return httpx.Response(200, json={"ok": True, "result": pending})
        if method == "getChatMember":
            status = self.members.get((int(request.url.params["chat_id"]), int(request.url.params["user_id"])), "left")
            return httpx.Response(200, json={"ok": True, "result": {"status": status}})
        if method == "sendPhoto":  # multipart: chat_id, caption, parse_mode, photo
            fields = dict(re.findall(rb'name="(\w+)"\r\n\r\n(.*?)\r\n--', request.content, re.S))
            chat_id, text = int(fields[b"chat_id"]), fields[b"caption"].decode()
            if self.refuse_photos:
                return httpx.Response(400, json={"ok": False, "description": "Bad Request: PHOTO_INVALID_DIMENSIONS"})
        else:
            chat_id, text = json.loads(request.content)["chat_id"], json.loads(request.content)["text"]
        if chat_id in self.moved:
            return httpx.Response(400, json={"ok": False, "description": "Bad Request: group chat was upgraded to a supergroup chat",
                                             "parameters": {"migrate_to_chat_id": self.moved[chat_id]}})
        if chat_id in self.removed:
            return httpx.Response(403, json={"ok": False, "description": "Forbidden: bot was kicked from the supergroup chat"})
        self.sent.append(int(chat_id))
        self.calls.append((method, int(chat_id), text))
        return httpx.Response(200, json={"ok": True, "result": {}})


@pytest.fixture
def api(db_engine, monkeypatch):
    fake = FakeTelegram()
    monkeypatch.setattr(httpx, "AsyncClient", lambda **kw: RealClient(transport=httpx.MockTransport(fake.handle), **kw))
    monkeypatch.setattr(telegram_group, "AsyncSessionLocal", async_sessionmaker(bind=db_engine, expire_on_commit=False))
    monkeypatch.setattr(telegram_group, "_offset", None)
    monkeypatch.setattr(telegram_group, "_found", None)
    settings = get_settings()
    monkeypatch.setattr(settings, "telegram_bot_token", "123:abc")
    monkeypatch.setattr(settings, "telegram_chat_id", str(OWNER))
    monkeypatch.setattr(settings, "telegram_group_name", None)
    return fake


def test_the_group_is_the_one_named_on_15_oct_unless_set():
    assert name_matches("Open Interest Bot", None) and name_matches("  open interest BOT ", None)
    assert not name_matches("Open Interest", None) and not name_matches(None, None)
    assert name_matches("Desk Alerts", "desk alerts") and not name_matches("Open Interest Bot", "Desk Alerts")


def test_groups_come_from_the_bots_additions_messages_and_upgrades():
    groups, moves = groups_in_updates([
        added(1, GROUP, "Open Interest Bot"),
        posted(2, OTHER_GROUP, "Family", migrate_to_chat_id=SUPERGROUP),
        {"update_id": 3, "message": {"chat": {"id": OWNER, "type": "private"}, "text": "/start"}},
    ])
    assert groups == {GROUP: {"type": "supergroup", "title": "Open Interest Bot", "bot_status": "administrator"},
                      OTHER_GROUP: {"type": "group", "title": "Family"}}
    assert moves == [(OTHER_GROUP, SUPERGROUP)]


def test_only_the_scanner_sends_to_the_group_from_the_runner():
    assert sends_to_group('"""\nF&O Opening-Candle Momentum Scanner -- FLY OI SCN, version 6 (native)\n')
    assert not sends_to_group('"""\nNifty PCR Futures Hedge -- NIFTY futures\n') and not sends_to_group(None)


async def test_an_alert_for_the_group_goes_to_the_private_chat_and_the_group(api, db_session):
    api.updates = [added(10, GROUP, "Open Interest Bot")]
    api.members[(GROUP, OWNER)] = "creator"
    await send_telegram("NIFTY OI levels 13:15", "table", to_group=True)
    assert api.sent == [OWNER, GROUP]
    [row] = (await db_session.execute(select(TelegramChat))).scalars().all()
    assert (row.chat_id, row.title, row.bot_status) == (GROUP, "Open Interest Bot", "administrator")

    await send_telegram("NIFTY OI levels 13:30", "table", to_group=True)  # found once, reused
    assert api.sent == [OWNER, GROUP, OWNER, GROUP] and api.offsets == [None]


async def test_other_alerts_stay_private(api):
    api.updates = [added(10, GROUP, "Open Interest Bot")]
    api.members[(GROUP, OWNER)] = "creator"
    await send_telegram("Zerodha login", "log in by 09:00")
    assert api.sent == [OWNER] and api.offsets == []


async def test_the_scanners_alerts_go_to_the_group_through_the_runners_context(api):
    api.updates = [added(10, GROUP, "Open Interest Bot")]
    api.members[(GROUP, OWNER)] = "member"
    token = telegram_to_group.set(True)
    try:
        await send_telegram("FLY OI SCN: 3 on the 9:20 watchlist", "rows")
    finally:
        telegram_to_group.reset(token)
    assert api.sent == [OWNER, GROUP]


async def test_a_group_of_that_name_without_the_owner_gets_nothing(api):
    api.updates = [added(10, OTHER_GROUP, "Open Interest Bot"), added(11, GROUP, "Open Interest Bot")]
    api.members[(GROUP, OWNER)] = "member"  # the owner is only in GROUP
    await send_telegram("NIFTY OI levels", "table", to_group=True)
    assert api.sent == [OWNER, GROUP]

    api.members.clear()
    telegram_group._found = None
    await send_telegram("NIFTY OI levels", "table", to_group=True)
    assert api.sent == [OWNER, GROUP, OWNER]  # neither has the owner now: private only


async def test_an_upgraded_group_gets_the_alert_at_its_new_id(api, db_session):
    api.updates = [posted(10, GROUP, "Open Interest Bot")]
    api.members[(GROUP, OWNER)] = "creator"
    api.moved[GROUP] = SUPERGROUP
    await send_telegram("NIFTY OI levels", "table", to_group=True)
    assert api.sent == [OWNER, SUPERGROUP]
    old = await db_session.get(TelegramChat, GROUP)
    assert old.migrated_to == SUPERGROUP and await db_session.get(TelegramChat, SUPERGROUP) is not None

    await send_telegram("NIFTY OI levels", "table", to_group=True)
    assert api.sent == [OWNER, SUPERGROUP, OWNER, SUPERGROUP]


async def test_a_bot_removed_from_the_group_keeps_the_alerts_private(api, db_session):
    api.updates = [added(10, GROUP, "Open Interest Bot")]
    api.members[(GROUP, OWNER)] = "creator"
    api.removed.add(GROUP)
    await send_telegram("NIFTY OI levels", "table", to_group=True)
    assert api.sent == [OWNER]
    assert (await db_session.get(TelegramChat, GROUP)).bot_status == "left"

    await send_telegram("NIFTY OI levels", "table", to_group=True)
    assert api.sent == [OWNER, OWNER] and api.offsets == [None, 11]  # looked again, each update read once


async def test_a_failing_group_never_costs_the_private_message(api, monkeypatch):
    async def broken(*args, **kwargs):
        raise RuntimeError("database down")

    monkeypatch.setattr(telegram, "send_to_group", broken)
    await send_telegram("NIFTY OI levels", "table", to_group=True)
    assert api.sent == [OWNER]


PNG = b"\x89PNG\r\n\x1a\n" + b"chart" * 50


async def test_a_picture_goes_with_the_text_as_its_caption_to_both_chats(api):
    api.updates = [added(10, GROUP, "Open Interest Bot")]
    api.members[(GROUP, OWNER)] = "creator"
    await send_telegram("🟢 Nifty PCR Futures: trade closed", "Net P&L   +1,000.00", to_group=True, photo=PNG)
    caption = "<b>🟢 Nifty PCR Futures: trade closed</b>\n<pre>Net P&amp;L   +1,000.00</pre>"
    assert api.calls == [("sendPhoto", OWNER, caption), ("sendPhoto", GROUP, caption)]


async def test_a_picture_telegram_refuses_still_delivers_the_text(api):
    api.updates = [added(10, GROUP, "Open Interest Bot")]
    api.members[(GROUP, OWNER)] = "creator"
    api.refuse_photos = True
    await send_telegram("trade taken", "rows", to_group=True, photo=PNG)
    assert [(method, chat) for method, chat, _ in api.calls] == [("sendMessage", OWNER), ("sendMessage", GROUP)]


async def test_text_too_long_for_a_caption_follows_the_picture(api):
    await send_telegram("trade closed", "x" * 1100, photo=PNG)
    assert [(method, text[:20]) for method, _, text in api.calls] == [("sendPhoto", "<b>trade closed</b>"), ("sendMessage", "<pre>xxxxxxxxxxxxxxx")]
