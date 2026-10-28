"""
52 Candle Breakout 15 MIN -- 500 NSE stocks on 15-minute candles (native, unsandboxed)
=======================================================================================

As agreed on 28-Oct-2026:

  Universe: STOCK_UNIVERSE, the weekly Nifty RS Rotation's 500 stocks, each
            on its own 15-minute candles. Long only, bought in cash.
  Entry:    a finished 15-minute candle closes above the highest high of the
            BREAKOUT_CANDLES (52) candles before it -- a breakout. RANK_AFTER
            (4 minutes) after each close, that candle's breakouts are ranked
            by how far the close is above that high (%), strongest first, and
            bought at the live price while a slot is free. A stock whose
            candle comes in later is bought then if a slot is still free; a
            breakout not bought before the next candle closes is passed
            over. The 15:15-15:30 candle closes with the market: not bought.
  Exit:     a finished 15-minute candle, closed after the buy, closes below
            the stock's own MA -- the average over its last MA_CANDLES (10)
            candles of (open + high + low + close) / 4, that candle included
            -- sold at the live price. Found on the day's last candle, it is
            sold at the next session's first price.
  Holding:  across days, until that exit. No stop-loss, no square-off.
  Size:     up to MAX_POSITIONS (10) stocks, each 1/MAX_POSITIONS of the
            pool's equity (cash + holdings at market) when bought, never
            more than the cash left.

Candles: the stocks' finished 15-minute candles, read for all of them in one
query (_bars) -- about once a minute while a candle's breakouts are being
decided, not every run. Live, the strategy asks the candle sync to keep all
of them fresh (note_native_candle_demand, as ctx.get_candles does); they
come in from Kite within about 3 minutes of each close, hence RANK_AFTER. A
stock with too few candles (a new listing) is skipped.

Runs through services/paper_trading/native_runner.py (ctx.get_price,
ctx.open_leg, ctx.close_leg, ctx.record_trade, ctx.note, ctx.wake_at). Stop
-> "Exit positions" sells every holding at live prices (state["force_exit"]).
"""

import uuid
from datetime import date, datetime, time, timedelta, timezone

from sqlalchemy import func, select

from app.core.time import as_aware_utc
from app.models.instrument import Instrument
from app.models.market_data import OhlcvCandle
from app.services.broker.zerodha_broker import IST
from app.services.market_data.active_timeframe_sync_scheduler import (
    note_native_candle_demand,
)
from app.services.market_data.nse_holidays import is_trading_holiday

TIMEFRAME = "15m"
BAR = timedelta(minutes=15)
BREAKOUT_CANDLES = 52
MA_CANDLES = 10
MAX_POSITIONS = 10
RANK_AFTER = timedelta(minutes=4)  # a candle's breakouts are ranked this long after it closes
SCAN_EVERY = timedelta(seconds=60)  # the 500 stocks' candles are read at most this often
HISTORY = timedelta(days=14)  # how far back the candles are read (52 candles is about 2 sessions)
SESSION_OPEN, SESSION_CLOSE = time(9, 15), time(15, 30)

STOCK_UNIVERSE = [  # the weekly Nifty RS Rotation's 500 stocks, copied 28-Oct-2026
    "360ONE", "3MINDIA", "AADHARHFC", "AARTIIND", "AAVAS", "ABB", "ABBOTINDIA", "ABCAPITAL", "ABDL", "ABFRL",
    "ABLBL", "ABREL", "ABSLAMC", "ACC", "ACE", "ACMESOLAR", "ACUTAAS", "ADANIENSOL", "ADANIENT", "ADANIGREEN",
    "ADANIPORTS", "ADANIPOWER", "AEGISLOG", "AEGISVOPAK", "AFCONS", "AFFLE", "AIAENG", "AIIL", "AJANTPHARM", "ALKEM",
    "AMBER", "AMBUJACEM", "ANANDRATHI", "ANANTRAJ", "ANGELONE", "ANTHEM", "ANURAS", "APARINDS", "APLAPOLLO",
    "APOLLOHOSP", "APOLLOTYRE", "APTUS", "ARE&M", "ASAHIINDIA", "ASHOKLEY", "ASIANPAINT", "ASTERDM", "ASTRAL",
    "ATGL", "ATHERENERG", "ATUL", "AUBANK", "AUROPHARMA", "AWL", "AXISBANK", "BAJAJ-AUTO", "BAJAJFINSV", "BAJAJHFL",
    "BAJAJHLDNG", "BAJFINANCE", "BALKRISIND", "BALRAMCHIN", "BANDHANBNK", "BANKBARODA", "BANKINDIA", "BATAINDIA",
    "BAYERCROP", "BBTC", "BDL", "BEL", "BELRISE", "BEML", "BERGEPAINT", "BHARATFORG", "BHARTIARTL", "BHARTIHEXA",
    "BHEL", "BIKAJI", "BIOCON", "BLS", "BLUEDART", "BLUEJET", "BLUESTARCO", "BOSCHLTD", "BPCL", "BRIGADE",
    "BRITANNIA", "BSE", "BSOFT", "CAMS", "CANBK", "CANFINHOME", "CANHLIFE", "CAPLIPOINT", "CARBORUNIV", "CARTRADE",
    "CASTROLIND", "CCL", "CDSL", "CEATLTD", "CEMPRO", "CENTRALBK", "CESC", "CGCL", "CGPOWER", "CHALET", "CHAMBLFERT",
    "CHENNPETRO", "CHOICEIN", "CHOLAFIN", "CHOLAHLDNG", "CIEINDIA", "CIPLA", "CLEAN", "COALINDIA", "COCHINSHIP",
    "COFORGE", "COHANCE", "COLPAL", "CONCOR", "CONCORDBIO", "COROMANDEL", "CPPLUS", "CRAFTSMAN", "CREDITACC",
    "CRISIL", "CROMPTON", "CUB", "CUMMINSIND", "CYIENT", "DABUR", "DALBHARAT", "DATAPATTNS", "DCMSHRIRAM",
    "DEEPAKFERT", "DEEPAKNTR", "DELHIVERY", "DEVYANI", "DIVISLAB", "DIXON", "DLF", "DMART", "DOMS", "DRREDDY",
    "ECLERX", "EICHERMOT", "EIDPARRY", "EIHOTEL", "ELECON", "ELGIEQUIP", "EMAMILTD", "EMCURE", "EMMVEE", "ENDURANCE",
    "ENGINERSIN", "ENRIN", "ERIS", "ESCORTS", "ETERNAL", "EXIDEIND", "FACT", "FEDERALBNK", "FINCABLES", "FIRSTCRY",
    "FIVESTAR", "FLUOROCHEM", "FORCEMOT", "FORTIS", "FSL", "GABRIEL", "GAIL", "GALLANTT", "GESHIP", "GICRE",
    "GILLETTE", "GLAND", "GLAXO", "GLENMARK", "GMDCLTD", "GMRAIRPORT", "GODFRYPHLP", "GODIGIT", "GODREJCP",
    "GODREJIND", "GODREJPROP", "GPIL", "GRANULES", "GRAPHITE", "GRASIM", "GRAVITA", "GROWW", "GRSE", "GVT&D", "HAL",
    "HAVELLS", "HBLENGINE", "HCLTECH", "HDBFS", "HDFCAMC", "HDFCBANK", "HDFCLIFE", "HEGAM", "HEROMOTOCO", "HEXT",
    "HFCL", "HINDALCO", "HINDCOPPER", "HINDPETRO", "HINDUNILVR", "HINDZINC", "HOMEFIRST", "HONASA", "HONAUT", "HSCL",
    "HUDCO", "HYUNDAI", "ICICIAMC", "ICICIBANK", "ICICIGI", "ICICIPRULI", "IDBI", "IDEA", "IDFCFIRSTB", "IEX",
    "IFCI", "IGIL", "IGL", "IIFL", "IKS", "INDGN", "INDHOTEL", "INDIACEM", "INDIAMART", "INDIANB", "INDIGO",
    "INDUSINDBK", "INDUSTOWER", "INFY", "INOXWIND", "INTELLECT", "IOB", "IOC", "IPCALAB", "IRB", "IRCON", "IRCTC",
    "IREDA", "IRFC", "ITC", "ITCHOTELS", "ITI", "J&KBANK", "JAINREC", "JBMA", "JINDALSAW", "JINDALSTEL", "JIOFIN",
    "JKCEMENT", "JKTYRE", "JMFINANCIL", "JPPOWER", "JSL", "JSWCEMENT", "JSWDULUX", "JSWENERGY", "JSWINFRA",
    "JSWSTEEL", "JUBLFOOD", "JUBLINGREA", "JUBLPHARMA", "JWL", "JYOTICNC", "KAJARIACER", "KALYANKJIL", "KARURVYSYA",
    "KAYNES", "KEC", "KEI", "KFINTECH", "KIMS", "KIRLOSENG", "KOTAKBANK", "KPIL", "KPITTECH", "KPRMILL",
    "LALPATHLAB", "LATENTVIEW", "LAURUSLABS", "LEMONTREE", "LENSKART", "LGEINDIA", "LICHSGFIN", "LICI", "LINDEINDIA",
    "LLOYDSME", "LODHA", "LT", "LTF", "LTFOODS", "LTM", "LTTS", "LUPIN", "M&M", "M&MFIN", "MAHABANK", "MANAPPURAM",
    "MANKIND", "MAPMYINDIA", "MARICO", "MARUTI", "MAXHEALTH", "MAZDOCK", "MCX", "MEDANTA", "MEESHO", "MFSL", "MGL",
    "MINDACORP", "MMTC", "MOTHERSON", "MOTILALOFS", "MPHASIS", "MRF", "MRPL", "MSUMI", "MUTHOOTFIN", "NAM-INDIA",
    "NATCOPHARM", "NATIONALUM", "NAUKRI", "NAVA", "NAVINFLUOR", "NBCC", "NCC", "NESTLEIND", "NETWEB", "NEULANDLAB",
    "NEWGEN", "NH", "NHPC", "NIACL", "NIVABUPA", "NLCINDIA", "NMDC", "NSLNISP", "NTPC", "NTPCGREEN", "NUVAMA",
    "NUVOCO", "NYKAA", "OBEROIRLTY", "OFSS", "OIL", "OLAELEC", "OLECTRA", "ONESOURCE", "ONGC", "PAGEIND", "PARADEEP",
    "PATANJALI", "PAYTM", "PCBL", "PERSISTENT", "PETRONET", "PFC", "PFIZER", "PFOCUS", "PGEL", "PHOENIXLTD",
    "PIDILITIND", "PIIND", "PINELABS", "PIRAMALFIN", "PNB", "PNBHOUSING", "POLICYBZR", "POLYCAB", "POLYMED",
    "POONAWALLA", "POWERGRID", "POWERINDIA", "PPLPHARMA", "PREMIERENE", "PRESTIGE", "PTCIL", "PVRINOX", "PWL",
    "RADICO", "RAILTEL", "RAINBOW", "RAMCOCEM", "RBLBANK", "RECLTD", "REDINGTON", "RELIANCE", "RHIM", "RITES",
    "RKFORGE", "RPOWER", "RRKABEL", "RVNL", "SAGILITY", "SAIL", "SAILIFE", "SAMMAANCAP", "SAPPHIRE", "SARDAEN",
    "SAREGAMA", "SBFC", "SBICARD", "SBILIFE", "SBIN", "SCHAEFFLER", "SCHNEIDER", "SCI", "SHREECEM", "SHRIRAMFIN",
    "SHYAMMETL", "SIEMENS", "SIGNATURE", "SJVN", "SOBHA", "SOLARINDS", "SONACOMS", "SONATSOFTW", "SPLPETRO", "SRF",
    "STARHEALTH", "SUMICHEM", "SUNDARMFIN", "SUNPHARMA", "SUNTV", "SUPREMEIND", "SUZLON", "SWANCORP", "SWIGGY",
    "SYNGENE", "SYRMA", "TARIL", "TATACAP", "TATACHEM", "TATACOMM", "TATACONSUM", "TATAELXSI", "TATAINVEST",
    "TATAPOWER", "TATASTEEL", "TATATECH", "TBOTEK", "TCS", "TECHM", "TECHNOE", "TEGA", "TEJASNET", "TENNIND",
    "THELEELA", "THERMAX", "TIINDIA", "TIMKEN", "TITAGARH", "TITAN", "TMCV", "TMPV", "TORNTPHARM", "TORNTPOWER",
    "TRAVELFOOD", "TRENT", "TRIDENT", "TRITURBINE", "TTML", "TVSMOTOR", "UBL", "UCOBANK", "ULTRACEMCO", "UNIONBANK",
    "UNITDSPR", "UNOMINDA", "UPL", "URBANCO", "USHAMART", "UTIAMC", "VBL", "VEDL", "VIJAYA", "VMM", "VOLTAS", "VTL",
    "WAAREEENER", "WELCORP", "WELSPUNLIV", "WHIRLPOOL", "WIPRO", "WOCKPHARMA", "YESBANK", "ZEEL", "ZENSARTECH",
    "ZENTEC", "ZFCVINDIA", "ZYDUSLIFE", "ZYDUSWELL",
]


def _is_trading_day(d: date) -> bool:
    return d.weekday() < 5 and not is_trading_holiday(d)


def market_open(now_ist: datetime) -> bool:
    return _is_trading_day(now_ist.date()) and SESSION_OPEN <= now_ist.time() < SESSION_CLOSE


def typical(bar: dict) -> float:
    return (bar["open"] + bar["high"] + bar["low"] + bar["close"]) / 4


def moving_average(bars: list[dict]) -> float | None:
    """The MA of the last MA_CANDLES bars' (open + high + low + close) / 4."""
    if len(bars) < MA_CANDLES:
        return None
    return sum(typical(bar) for bar in bars[-MA_CANDLES:]) / MA_CANDLES


def breakout(bars: list[dict]) -> float | None:
    """How far (%) the newest bar closed above the highest high of the
    BREAKOUT_CANDLES bars before it, when it did; else None."""
    if len(bars) < BREAKOUT_CANDLES + 1:
        return None
    high = max(bar["high"] for bar in bars[-BREAKOUT_CANDLES - 1:-1])
    close = bars[-1]["close"]
    return (close / high - 1) * 100 if close > high > 0 else None


def last_close(now_ist: datetime) -> datetime | None:
    """The end of the newest 15-minute candle finished by `now_ist` within
    today's session (09:30 .. 15:30), or None before 09:30."""
    opening = datetime.combine(now_ist.date(), SESSION_OPEN, tzinfo=IST)
    if now_ist < opening + BAR:
        return None
    ended = opening + ((min(now_ist, datetime.combine(now_ist.date(), SESSION_CLOSE, tzinfo=IST)) - opening) // BAR) * BAR
    return ended


def _next_ranking(now_ist: datetime, closed: datetime | None) -> str:
    """When the next candle's breakouts are ranked, for the card."""
    session_close = datetime.combine(now_ist.date(), SESSION_CLOSE, tzinfo=IST)
    first = datetime.combine(now_ist.date(), SESSION_OPEN, tzinfo=IST) + BAR + RANK_AFTER
    if _is_trading_day(now_ist.date()):
        if closed is None:
            return f"{first:%H:%M}"
        if now_ist < closed + RANK_AFTER and closed < session_close:
            return f"{closed + RANK_AFTER:%H:%M}"
        if closed + BAR < session_close:
            return f"{closed + BAR + RANK_AFTER:%H:%M}"
    return f"{first:%H:%M} on the next trading day"


async def _bars(ctx, ids: list[uuid.UUID], count: int) -> dict[uuid.UUID, list[dict]]:
    """Each instrument's newest `count` finished 15-minute candles, oldest
    first -- one query for all of them."""
    if not ids:
        return {}
    newest_start = ctx.now - BAR  # a candle starting after this hasn't finished
    ranked = (
        select(
            OhlcvCandle.instrument_id, OhlcvCandle.ts, OhlcvCandle.open, OhlcvCandle.high, OhlcvCandle.low, OhlcvCandle.close,
            func.row_number().over(partition_by=OhlcvCandle.instrument_id, order_by=OhlcvCandle.ts.desc()).label("n"),
        )
        .where(
            OhlcvCandle.instrument_id.in_(ids), OhlcvCandle.timeframe == TIMEFRAME,
            OhlcvCandle.ts >= ctx.now - HISTORY, OhlcvCandle.ts <= newest_start,
        )
        .subquery()
    )
    rows = (await ctx.db.execute(select(ranked).where(ranked.c.n <= count))).all()
    out: dict[uuid.UUID, list[dict]] = {}
    for row in rows:
        out.setdefault(row.instrument_id, []).append(
            {"ts": as_aware_utc(row.ts), "open": row.open, "high": row.high, "low": row.low, "close": row.close}
        )
    for bars in out.values():
        bars.sort(key=lambda bar: bar["ts"])
    return out


async def _sell(ctx, symbol: str, leg: dict, price: float, exit_reason: str) -> float:
    instrument = await ctx.db.get(Instrument, uuid.UUID(leg["instrument_id"]))
    await ctx.close_leg(instrument, "sell", leg["quantity"], price)
    pnl = (price - leg["entry_price"]) * leg["quantity"]
    await ctx.record_trade(
        legs=[{
            "instrument_id": leg["instrument_id"], "side": "long", "quantity": leg["quantity"],
            "entry_price": leg["entry_price"], "exit_price": price,
        }],
        pnl=pnl, pnl_pct=(pnl / (leg["entry_price"] * leg["quantity"]) * 100) if leg["entry_price"] else 0.0,
        exit_reason=exit_reason, opened_at=datetime.fromisoformat(leg["opened_at"]),
    )
    return pnl


async def evaluate(ctx) -> None:
    now_ist = ctx.now.astimezone(IST)
    holdings: dict = ctx.state.setdefault("holdings", {})  # {symbol: {instrument_id, quantity, entry_price, opened_at, ...}}
    force_exit = ctx.state.pop("force_exit", False)
    is_open = market_open(now_ist)
    notes: list[str] = []

    universe = {
        i.symbol: i for i in (
            await ctx.db.execute(select(Instrument).where(Instrument.exchange == "NSE", Instrument.symbol.in_(STOCK_UNIVERSE)))
        ).scalars().all()
    }
    if ctx.started_at is not None:  # live: keep all of them synced (a backtest reads what's stored)
        for instrument in universe.values():
            note_native_candle_demand(instrument.id, TIMEFRAME, ctx.now)

    # --- Exits: stop -> "Exit positions", or a candle closed after the buy below the MA.
    sold = []
    held_bars = await _bars(ctx, [uuid.UUID(leg["instrument_id"]) for leg in holdings.values()], MA_CANDLES)
    for symbol, leg in list(holdings.items()):
        reason = "manual" if force_exit else None
        if reason is None:
            bars = held_bars.get(uuid.UUID(leg["instrument_id"]), [])
            ma = moving_average(bars)
            if ma is not None and bars[-1]["ts"] + BAR > datetime.fromisoformat(leg["opened_at"]) and bars[-1]["close"] < ma:
                reason = "ma_exit"
                leg["exit_due"] = f"{(bars[-1]['ts'] + BAR).astimezone(IST):%d-%b %H:%M} close {bars[-1]['close']:,.2f} < MA {ma:,.2f}"
        if reason is None:
            continue
        if not is_open and not force_exit:
            notes.append(f"{symbol} sells at the open ({leg['exit_due']})")
            continue
        price = await ctx.get_price(uuid.UUID(leg["instrument_id"]))
        if price is None:
            notes.append(f"{symbol}: exit due, no live price yet")
            continue
        holdings.pop(symbol)
        pnl = await _sell(ctx, symbol, leg, price, reason)
        sold.append(f"{symbol} @ {price:,.2f} ({'stop' if reason == 'manual' else leg['exit_due']}, P&L {pnl:+,.0f})")
    if force_exit:
        ctx.state["holdings"] = holdings
        ctx.note("exited", signal="COVER", reason=f"manual exit: sold {len(sold)}" + (f" | {'; '.join(notes)}" if notes else ""))
        return

    # --- Entries: the newest finished candle's breakouts, strongest first.
    bought, passed = [], []
    closed = last_close(now_ist) if _is_trading_day(now_ist.date()) else None
    tradable = closed is not None and closed.time() < SESSION_CLOSE  # the 15:15-15:30 candle isn't bought
    if closed is not None:
        if now_ist < closed + RANK_AFTER:
            ctx.wake_at(closed + RANK_AFTER)  # rank on the dot
        elif closed + BAR > now_ist and now_ist.time() < SESSION_CLOSE:
            ctx.wake_at(min(now_ist + SCAN_EVERY, closed + BAR))
    scan_due = (
        tradable and is_open and closed + RANK_AFTER <= now_ist < closed + BAR and len(holdings) < MAX_POSITIONS
        and (ctx.state.get("_scanned_at") is None or ctx.now - datetime.fromisoformat(ctx.state["_scanned_at"]) >= SCAN_EVERY)
    )
    if scan_due:
        ctx.state["_scanned_at"] = ctx.now.isoformat()
        candle_start = (closed - BAR).astimezone(timezone.utc)
        found = []
        bars_by_id = await _bars(ctx, [i.id for s, i in universe.items() if s not in holdings], BREAKOUT_CANDLES + 1)
        for symbol, instrument in universe.items():
            bars = bars_by_id.get(instrument.id)
            if symbol in holdings or not bars or bars[-1]["ts"] != candle_start:
                continue  # held, or this candle not in yet
            strength = breakout(bars)
            if strength is not None:
                found.append((strength, symbol))
        found.sort(key=lambda x: (-x[0], x[1]))
        equity = ctx.portfolio.cash
        for leg in holdings.values() if found else []:
            equity += ((await ctx.get_price(uuid.UUID(leg["instrument_id"]))) or leg["entry_price"]) * leg["quantity"]
        for strength, symbol in found:
            if len(holdings) >= MAX_POSITIONS:
                passed.append(symbol)
                continue
            instrument = universe[symbol]
            price = await ctx.get_price(instrument.id)
            if price is None or price <= 0:
                continue
            quantity = float(int(min(equity / MAX_POSITIONS, ctx.portfolio.cash) / price))
            if quantity <= 0:
                passed.append(symbol)
                continue
            await ctx.open_leg(instrument, "buy", quantity, price)
            holdings[symbol] = {
                "instrument_id": str(instrument.id), "quantity": quantity, "entry_price": price,
                "opened_at": ctx.now.isoformat(), "breakout_pct": round(strength, 2),
                "breakout_candle": candle_start.isoformat(),
            }
            bought.append(f"{symbol} @ {price:,.2f} (+{strength:.2f}% over its 52-candle high)")

    ctx.state["holdings"] = holdings
    text = []
    if sold:
        text.append("sold " + "; ".join(sold))
    if bought:
        text.append("bought " + "; ".join(bought))
    if passed:
        text.append(f"no slot for {len(passed)} more breakout(s): {', '.join(passed[:5])}{'...' if len(passed) > 5 else ''}")
    text.extend(notes)
    text.append(f"holding {len(holdings)}/{MAX_POSITIONS} | {len(universe)} stocks watched | next ranking {_next_ranking(now_ist, closed)}")
    action = "entered" if bought else "exited" if sold else "hold" if holdings else "skipped"
    signal = "BUY" if bought else "SELL" if sold else None
    ctx.note(action, signal=signal, reason=" | ".join(text))
