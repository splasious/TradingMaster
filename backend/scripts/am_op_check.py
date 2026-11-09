"""Before a take-profit / stop-loss rule on AM OP TRD 15 MIN (9 Nov): read-only facts.

AM OP TRD 15 MIN's running code is only in the app's database, so a change
goes in as edits to it (as alembic d1f3a5c7e9b2 did). This says, without
printing any of the code:

- its versions (number, md5) and whether the newest is what the last edit
  migration left (d1f3a5c7e9b2 records no TO_MD5, so: the md5s only);
- its paper card(s) and live run(s): status, version, open position yes/no
  and which kind (straddle / spreads);
- how many times each place a new rule would hook into appears in the
  newest code, and whether the names it would add are already taken.

Run inside the backend container (the health check's `am_op_check` input
pipes it to `python -`). Counts, md5s and yes/no only -- no code, no user,
no P&L, no sizes.
"""

import asyncio
import hashlib

from sqlalchemy import func, select

from app.db.session import AsyncSessionLocal
from app.models.live_native import LiveNativeDeployment
from app.models.paper_trading import PaperNativeDeployment, PaperNativeTrade
from app.models.strategy import Strategy, StrategyVersion
from app.services.broker.zerodha_broker import IST

NAME = "AM OP TRD 15 MIN"
ANCHORS = {
    "close_position def": '    async def close_position(exit_reason: str) -> None:\n',
    "close_position clears position": '        ctx.state["position"] = None\n',
    "manual exit": '        if force_exit:\n            await close_position("manual")\n',
    "15:00 cutoff check": '        if past_exit_cutoff(now_ist.time()):\n',
    "PCR exit read": '        pcr = await ctx.get_pcr(timeframe=PCR_TIMEFRAME)\n        if pcr is not None:\n            exit_below, exit_above = pcr_exit_band(regime)\n',
    "MA exit check": '        if ma is not None and ma_exit_due(regime, newest, ma, datetime.fromisoformat(position["opened_at"])):\n',
    "roll check": '        if entry_spot is not None and moved >= ROLL_TRIGGER:\n',
    "entry window check": '    if not in_entry_window(now_ist.time()):\n',
    "determine_regime def": 'def determine_regime(pcr: float) -> str:',
    "exit price from LTP": '            exit_price = await ctx.get_price(instrument.id) if instrument else None\n',
    "MA stop read": '    stop = ctx.state.get("_ma_stop")\n',
}
NAMES = ("TAKE_PROFIT", "STOP_LOSS", "_open_pnl", "_pnl_stop", "PNL_STOP")


def md5(code: str | None) -> str:
    return hashlib.md5((code or "").replace("\r", "").encode()).hexdigest()


async def main() -> None:
    async with AsyncSessionLocal() as db:
        strategies = (await db.execute(select(Strategy).where(func.trim(Strategy.name) == NAME))).scalars().all()
        print(f"strategies named {NAME!r}: {len(strategies)}")
        for s in strategies:
            versions = (await db.execute(
                select(StrategyVersion).where(StrategyVersion.strategy_id == s.id).order_by(StrategyVersion.version_number)
            )).scalars().all()
            by_id = {v.id: v.version_number for v in versions}
            print(f"  versions: {', '.join(f'v{v.version_number} {md5(v.python_code)[:8]} {v.timeframe}' for v in versions)}")
            newest = versions[-1] if versions else None
            code = (newest.python_code or "").replace("\r", "") if newest else ""
            print(f"  newest v{newest.version_number if newest else '-'}: compiles {_compiles(code)}")
            for label, anchor in ANCHORS.items():
                print(f"    '{label}' found {code.count(anchor)}x")
            print(f"    names already used: {', '.join(n for n in NAMES if n in code) or 'none'}")
            cards = (await db.execute(select(PaperNativeDeployment).where(PaperNativeDeployment.strategy_id == s.id))).scalars().all()
            for card in cards:
                position = (card.state or {}).get("position")
                ran = card.last_evaluated_at.astimezone(IST).strftime("%d-%b %H:%M:%S") if card.last_evaluated_at else "never"
                kind = f" ({position.get('regime')}, {len(position.get('legs') or {})} legs)" if position else ""
                print(f"  paper card {str(card.id)[:8]}: {card.status} on v{by_id.get(card.strategy_version_id)}, last run {ran}, "
                      f"position open: {bool(position)}{kind}")
                trades = (await db.execute(
                    select(func.date(PaperNativeTrade.closed_at), func.count()).where(PaperNativeTrade.deployment_id == card.id)
                    .group_by(func.date(PaperNativeTrade.closed_at)).order_by(func.date(PaperNativeTrade.closed_at).desc()).limit(5)
                )).all()
                print(f"    closed trades, last 5 days with any: {', '.join(f'{d} x{n}' for d, n in trades) or 'none'}")
                reasons = (await db.execute(
                    select(PaperNativeTrade.exit_reason, func.count()).where(PaperNativeTrade.deployment_id == card.id)
                    .group_by(PaperNativeTrade.exit_reason).order_by(func.count().desc()).limit(8)
                )).all()
                print(f"    exit reasons (all time): {', '.join(f'{_reason(r)} x{n}' for r, n in reasons) or 'none'}")
                live = (await db.execute(
                    select(LiveNativeDeployment.status, LiveNativeDeployment.strategy_version_id)
                    .where(LiveNativeDeployment.paper_deployment_id == card.id)
                )).all()
                print(f"    live runs: {', '.join(f'{st} on v{by_id.get(vid)}' for st, vid in live) or 'none'}")


def _reason(reason: str | None) -> str:
    """"pcr_exit_0.845" -> "pcr_exit": the reading isn't needed here."""
    return "pcr_exit" if (reason or "").startswith("pcr_exit") else (reason or "-")


def _compiles(code: str) -> bool:
    try:
        compile(code, "<am-op>", "exec")
        return True
    except SyntaxError:
        return False


if __name__ == "__main__":
    asyncio.run(main())
