"""Which strategies' trades go to Telegram (5 Nov): read-only.

The running app's Settings.telegram_trade_alert_strategies -- its code
default, or the server's TELEGRAM_TRADE_ALERT_STRATEGIES if set there --
whether Telegram and the group are set up (yes/no), and for AM OP TRD 15
MIN and Conti Calendar 15 MIN: their cards' status and whether their own
code calls Telegram itself (yes/no -- the code is never printed).

Run inside the backend container (the health check's
`telegram_alert_check` input pipes it to `python -`). Strategy names,
counts and yes/no only.
"""

import asyncio
import os

from sqlalchemy import func, select

from app.core.config import Settings, get_settings
from app.db.session import AsyncSessionLocal
from app.models.paper_trading import PaperNativeDeployment
from app.models.strategy import Strategy, StrategyVersion

NAMES = ("AM OP TRD 15 MIN", "Conti Calendar 15 MIN")


async def main() -> None:
    settings = get_settings()
    default = Settings.model_fields["telegram_trade_alert_strategies"].default
    print(f"trade alerts go for: {settings.telegram_trade_alert_strategies!r} "
          f"(code default {default!r}; set on the server: {'TELEGRAM_TRADE_ALERT_STRATEGIES' in os.environ})")
    print(f"Telegram set up: {bool(settings.telegram_bot_token and settings.telegram_chat_id)}; "
          f"group name set on the server: {bool(settings.telegram_group_name)}")
    async with AsyncSessionLocal() as db:
        for name in NAMES:
            strategies = (await db.execute(select(Strategy).where(func.trim(Strategy.name) == name))).scalars().all()
            for s in strategies:
                newest = (await db.execute(
                    select(StrategyVersion).where(StrategyVersion.strategy_id == s.id)
                    .order_by(StrategyVersion.version_number.desc()).limit(1)
                )).scalars().first()
                cards = (await db.execute(
                    select(PaperNativeDeployment.status, StrategyVersion.version_number, StrategyVersion.python_code)
                    .join(StrategyVersion, StrategyVersion.id == PaperNativeDeployment.strategy_version_id)
                    .where(PaperNativeDeployment.strategy_id == s.id)
                )).all()
                print(f"{name}: newest v{newest.version_number if newest else '-'}; cards "
                      f"{', '.join(f'{st} on v{n}' for st, n, _ in cards) or 'none'}; its code calls Telegram itself: "
                      f"{', '.join(f'v{n} ' + str('telegram' in (code or '').lower()) for _, n, code in cards) or '-'}")


if __name__ == "__main__":
    asyncio.run(main())
