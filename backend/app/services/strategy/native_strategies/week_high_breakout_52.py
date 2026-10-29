"""
52 Week High Breakout -- 500 NSE stocks on weekly candles (native, unsandboxed)
================================================================================

As agreed on 29-Oct-2026, after the daily / weekly / monthly backtests: the
52 Candle Breakout 15 MIN card (version 1, 28 Oct, 15-minute candles) moved
to weekly candles and renamed.

  Universe: STOCK_UNIVERSE, the weekly Nifty RS Rotation's 500 stocks. Long
            only, bought in cash.
  Candles:  weekly, Monday-Friday, built from the stored daily candles. Only
            finished weeks count -- the week still running never does -- and
            a stock's week counts once its last trading day's candle is in.
  Entry:    a finished week closes above the highest high of the
            BREAKOUT_CANDLES (52) weeks before it -- a breakout. On the next
            week's first trading day, from RANK_AT (09:16, a minute after
            the open), the breakouts are ranked by how far the close is
            above that high (%), strongest first, and bought at the live
            price while a slot is free; the rest are passed over. A stock
            whose week isn't in yet is looked at again every SCAN_EVERY that
            day; a breakout not bought that day is passed over.
  Exit:     a finished week, closed after the buy, closes below the stock's
            own MA -- the average over its last MA_CANDLES (10) weeks of
            (open + high + low + close) / 4, that week included -- sold at
            the live price from RANK_AT on the next trading day, before that
            day's buys, so the slot is free for them.
  Holding:  until that exit. No stop-loss.
  Size:     up to MAX_POSITIONS (10) stocks, each 1/MAX_POSITIONS of the
            pool's equity (cash + holdings at market) when bought, never
            more than the cash left.
  Switch:   a stock version 1 bought on a 15-minute breakout (a holding with
            no "breakout_week") is sold at the live price from RANK_AT on
            the first trading day after the switch: the weekly rules start
            with their slots free.

Candles: the stored daily ones, read for all the stocks in one query (_weeks)
-- once on the ranking morning, then only the stocks whose week wasn't in
yet. No candle sync is asked for: the daily top-up (backfill_platform/
topup.py, after the close) stores the 500 stocks' days, as the RS Rotation
reads them.

Runs through services/paper_trading/native_runner.py (ctx.get_price,
ctx.open_leg, ctx.close_leg, ctx.record_trade, ctx.note, ctx.wake_at). Stop
-> "Exit positions" sells every holding at live prices (state["force_exit"]).
"""

import uuid
from datetime import date, datetime, time, timedelta, timezone

from sqlalchemy import select

from app.core.time import as_aware_utc
from app.models.instrument import Instrument
from app.models.market_data import OhlcvCandle
from app.services.broker.zerodha_broker import IST
from app.services.market_data.nse_holidays import is_trading_holiday

STORED = "1d"  # the weekly candles are built from these
BREAKOUT_CANDLES = 52
MA_CANDLES = 10
MAX_POSITIONS = 10
RANK_AT = time(9, 16)  # the week's sells and buys, at live prices, a minute after the open
SCAN_EVERY = timedelta(minutes=5)  # a stock whose week isn't in yet is looked at again this often
HISTORY_WEEKS = 56  # weeks of daily candles read: 53 finished weeks and slack for a week without trading
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


def monday(d: date) -> date:
    return d - timedelta(days=d.weekday())


def first_trading_day(week: date) -> date | None:
    """The first trading day of the week starting Monday `week`, or None."""
    return next((day for day in (week + timedelta(days=k) for k in range(5)) if _is_trading_day(day)), None)


def finished_before(now_ist: datetime) -> date:
    """The Monday every week before which has finished by `now_ist`: next
    week's once this week's last session has closed, else this week's."""
    week = monday(now_ist.date())
    days = [week + timedelta(days=k) for k in range(5)]
    left = [d for d in days if _is_trading_day(d) and (d > now_ist.date() or (d == now_ist.date() and now_ist.time() < SESSION_CLOSE))]
    return week + timedelta(weeks=1) if not left else week


def last_trading_day_before(d: date) -> date:
    d -= timedelta(days=1)
    while not _is_trading_day(d):
        d -= timedelta(days=1)
    return d


def weeks(days: list[dict]) -> list[dict]:
    """Daily bars (oldest first: day, open, high, low, close) as weekly ones,
    Monday-Friday: week (its Monday), last_day, open, high, low, close."""
    out: list[dict] = []
    for bar in days:
        week = monday(bar["day"])
        if out and out[-1]["week"] == week:
            last = out[-1]
            last["high"], last["low"] = max(last["high"], bar["high"]), min(last["low"], bar["low"])
            last["close"], last["last_day"] = bar["close"], bar["day"]
        else:
            out.append({"week": week, "last_day": bar["day"], "open": bar["open"], "high": bar["high"], "low": bar["low"],
                        "close": bar["close"]})
    return out


def _midnight(d: date) -> datetime:
    return datetime.combine(d, time(0), tzinfo=IST).astimezone(timezone.utc)


async def _weeks(ctx, ids: list[uuid.UUID], this_week: date) -> dict[uuid.UUID, list[dict]]:
    """Each instrument's weekly bars before the week starting `this_week`,
    oldest first, from its stored daily candles -- one query for all."""
    if not ids:
        return {}
    rows = await ctx.db.execute(
        select(OhlcvCandle.instrument_id, OhlcvCandle.ts, OhlcvCandle.open, OhlcvCandle.high, OhlcvCandle.low, OhlcvCandle.close)
        .where(OhlcvCandle.instrument_id.in_(ids), OhlcvCandle.timeframe == STORED,
               OhlcvCandle.ts >= _midnight(this_week - timedelta(weeks=HISTORY_WEEKS)), OhlcvCandle.ts < _midnight(this_week))
        .order_by(OhlcvCandle.instrument_id, OhlcvCandle.ts)
    )
    days: dict[uuid.UUID, list[dict]] = {}
    for instrument_id, ts, o, h, lo, c in rows:
        day = as_aware_utc(ts).astimezone(IST).date()
        bars = days.setdefault(instrument_id, [])
        if None in (o, h, lo, c) or (bars and bars[-1]["day"] == day):
            continue  # one candle a day
        bars.append({"day": day, "open": float(o), "high": float(h), "low": float(lo), "close": float(c)})
    return {instrument_id: weeks(bars) for instrument_id, bars in days.items()}


def _finished(bars: list[dict], this_week: date) -> bool:
    """The newest bar is last week's, its last trading day included."""
    return bool(bars) and bars[-1]["week"] == this_week - timedelta(weeks=1) and bars[-1]["last_day"] == last_trading_day_before(this_week)


def _next_ranking(now_ist: datetime) -> str:
    """When the next week's breakouts are ranked, for the card."""
    week = monday(now_ist.date())
    day = first_trading_day(week)
    if day is None or now_ist.date() > day or (now_ist.date() == day and now_ist.time() >= RANK_AT):
        day = None
        while day is None:
            week += timedelta(weeks=1)
            day = first_trading_day(week)
    return f"{day:%a %d-%b} {RANK_AT:%H:%M}"


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
    today = now_ist.date()
    this_week = monday(today)
    holdings: dict = ctx.state.setdefault("holdings", {})  # {symbol: {instrument_id, quantity, entry_price, opened_at, ...}}
    force_exit = ctx.state.pop("force_exit", False)
    is_open = market_open(now_ist)
    trading = is_open and now_ist.time() >= RANK_AT  # this morning's sells and buys are on
    if _is_trading_day(today) and now_ist.time() < RANK_AT:
        ctx.wake_at(datetime.combine(today, RANK_AT, tzinfo=IST))
    notes: list[str] = []

    # --- Exits: stop -> "Exit positions"; a version-1 holding (the switch);
    # or a finished week, closed after the buy, below the MA.
    sold = []
    finished = finished_before(now_ist)
    held_weeks = await _weeks(ctx, [uuid.UUID(leg["instrument_id"]) for leg in holdings.values() if "breakout_week" in leg], finished)
    for symbol, leg in list(holdings.items()):
        reason = "manual" if force_exit else None
        if reason is None and "breakout_week" not in leg:
            reason = "switch_to_weekly"
            leg["exit_due"] = "bought on 15-min candles, the strategy now on weekly ones"
        if reason is None:
            bars = held_weeks.get(uuid.UUID(leg["instrument_id"]), [])
            ma = moving_average(bars)
            week_closed = datetime.combine(bars[-1]["last_day"], SESSION_CLOSE, tzinfo=IST) if bars else None
            if (ma is not None and _finished(bars, finished) and week_closed > datetime.fromisoformat(leg["opened_at"])
                    and bars[-1]["close"] < ma):
                reason = "ma_exit"
                leg["exit_due"] = f"week of {bars[-1]['week']:%d-%b} closed {bars[-1]['close']:,.2f} < its 10-week MA {ma:,.2f}"
        if reason is None:
            continue
        if not trading and not force_exit:
            when = "today" if _is_trading_day(today) and now_ist.time() < RANK_AT else "on the next trading day"
            notes.append(f"{symbol} sells at {RANK_AT:%H:%M} {when} ({leg['exit_due']})")
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

    universe = {
        i.symbol: i for i in (
            await ctx.db.execute(select(Instrument).where(Instrument.exchange == "NSE", Instrument.symbol.in_(STOCK_UNIVERSE)))
        ).scalars().all()
    }

    # --- Entries: last week's breakouts, on this week's first trading day, strongest first.
    bought, passed = [], []
    ranking = ctx.state.get("ranking") or {}
    if ranking.get("week") != this_week.isoformat():
        ranking = {"week": this_week.isoformat(), "decided": [], "waiting": None, "scanned_at": None}
    scan_due = (
        trading and today == first_trading_day(this_week) and len(holdings) < MAX_POSITIONS and ranking["waiting"] != 0
        and (ranking["scanned_at"] is None or ctx.now - datetime.fromisoformat(ranking["scanned_at"]) >= SCAN_EVERY)
    )
    if scan_due:
        ranking["scanned_at"] = ctx.now.isoformat()
        decided = set(ranking["decided"])
        look = {s: i for s, i in universe.items() if s not in holdings and s not in decided}
        bars_by_id = await _weeks(ctx, [i.id for i in look.values()], this_week)
        found, waiting = [], 0
        for symbol, instrument in look.items():
            bars = bars_by_id.get(instrument.id, [])
            if not _finished(bars, this_week):
                if len(bars) < BREAKOUT_CANDLES:
                    decided.add(symbol)  # too few weeks to break out, whatever comes in
                else:
                    waiting += 1  # last week's candles not (all) in yet
                continue
            strength = breakout(bars)
            if strength is None:
                decided.add(symbol)
            else:
                found.append((strength, symbol))
        found.sort(key=lambda x: (-x[0], x[1]))
        equity = ctx.portfolio.cash
        for leg in holdings.values() if found else []:
            equity += ((await ctx.get_price(uuid.UUID(leg["instrument_id"]))) or leg["entry_price"]) * leg["quantity"]
        for strength, symbol in found:
            if len(holdings) >= MAX_POSITIONS:
                passed.append(symbol)
                decided.add(symbol)
                continue
            instrument = universe[symbol]
            price = await ctx.get_price(instrument.id)
            if price is None or price <= 0:
                waiting += 1
                continue
            quantity = float(int(min(equity / MAX_POSITIONS, ctx.portfolio.cash) / price))
            decided.add(symbol)
            if quantity <= 0:
                passed.append(symbol)
                continue
            await ctx.open_leg(instrument, "buy", quantity, price)
            holdings[symbol] = {
                "instrument_id": str(instrument.id), "quantity": quantity, "entry_price": price,
                "opened_at": ctx.now.isoformat(), "breakout_pct": round(strength, 2),
                "breakout_week": (this_week - timedelta(weeks=1)).isoformat(),
            }
            bought.append(f"{symbol} @ {price:,.2f} (+{strength:.2f}% over its 52-week high)")
        ranking["decided"], ranking["waiting"] = sorted(decided), waiting
        if waiting:
            ctx.wake_at(ctx.now + SCAN_EVERY)
    ctx.state["ranking"] = ranking
    if ranking["waiting"] and today == first_trading_day(this_week) and is_open:
        notes.append(f"{ranking['waiting']} stock(s)' last week not in yet: looked at again every {SCAN_EVERY.seconds // 60} min today")

    ctx.state["holdings"] = holdings
    text = []
    if sold:
        text.append("sold " + "; ".join(sold))
    if bought:
        text.append("bought " + "; ".join(bought))
    if passed:
        text.append(f"no slot for {len(passed)} more breakout(s): {', '.join(passed[:5])}{'...' if len(passed) > 5 else ''}")
    text.extend(notes)
    text.append(f"holding {len(holdings)}/{MAX_POSITIONS} | {len(universe)} stocks watched | next ranking {_next_ranking(now_ist)}")
    action = "entered" if bought else "exited" if sold else "hold" if holdings else "skipped"
    signal = "BUY" if bought else "SELL" if sold else None
    ctx.note(action, signal=signal, reason=" | ".join(text))
