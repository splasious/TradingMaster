from datetime import datetime

from sqlalchemy import BigInteger, DateTime, String
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


class TelegramChat(Base):
    """A Telegram group the app's bot has been seen in -- learnt from the
    bot's updates (services/notifications/telegram_group.py): Telegram
    tells a bot a group's ID only through them, never by its name.
    `bot_status` is the bot's own standing there ("member", "administrator",
    "left", "kicked"); `migrated_to` the ID a group took when Telegram
    upgraded it to a supergroup."""

    __tablename__ = "telegram_chats"

    chat_id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=False)
    type: Mapped[str] = mapped_column(String(20), nullable=False)
    title: Mapped[str | None] = mapped_column(String(255))
    bot_status: Mapped[str | None] = mapped_column(String(20))
    migrated_to: Mapped[int | None] = mapped_column(BigInteger)
    seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
