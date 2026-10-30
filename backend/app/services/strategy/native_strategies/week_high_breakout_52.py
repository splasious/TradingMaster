"""
52 Week High Breakout -- 500 NSE stocks on weekly candles (native, unsandboxed)
================================================================================

As agreed on 29-Oct-2026 (weekly candles, after the daily / weekly / monthly
backtests) and 30-Oct-2026 (the week's check on Friday at 3 PM).

  Universe: STOCK_UNIVERSE, the weekly Nifty RS Rotation's 500 stocks. Long
            only, bought in cash.
  Candles:  weekly, Monday-Friday, built from the stored daily candles. The
            week's check is on its CHECK day -- Friday, or the nearest
            trading day before it in the week when Friday is an NSE holiday
            -- at DECIDE_AT (15:00): this week's candle is then its days so
            far, today's from Kite's live quote (today's open, high and low
            so far), its close the live price at 15:00.
  Exit:     first, each stock held whose week at 15:00 closes below its own
            MA -- the average over its last MA_CANDLES (10) weeks of (open +
            high + low + close) / 4, this week included -- is sold at the
            live price. A stock is checked from the check day after the one
            it was bought on.
  Entry:    then each stock whose live price at 15:00 is above the highest
            high of the BREAKOUT_CANDLES (52) weeks before this week -- a
            breakout -- ranked by how far above (%), strongest first, is
            bought at the live price while a slot is free; the rest are
            passed over until next week's check.
  Holding:  until that exit. No stop-loss.
  Size:     up to MAX_POSITIONS (10) stocks, each 1/MAX_POSITIONS of the
            pool's equity (cash + holdings at market) when bought, never
            more than the cash left.

A stock without a quote yet, or whose earlier days aren't all stored, is
looked at again every RETRY_EVERY until the close; the check isn't made up
on another day. Nothing trades outside the check.

Candles: the stored daily ones, read for every stock in one query (_days) --
the daily top-up (backfill_platform/topup.py, after the close) stores the
500 stocks' days, as the RS Rotation reads them -- and one Kite quote call
for today's (kite_quotes, 500 a request). In a backtest the stored day
stands in for the quote.

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
from app.services.broker.kite_ticker_service import find_connected_zerodha_credentials
from app.services.broker.zerodha_broker import IST, ZerodhaKiteBroker
from app.services.fo_scan.pacing import kite_quotes
from app.services.market_data.nse_holidays import is_trading_holiday

STORED = "1d"  # the weekly candles are built from these
BREAKOUT_CANDLES = 52
MA_CANDLES = 10
MAX_POSITIONS = 10
DECIDE_AT = time(15, 0)  # the week's check: its close is the live price then
RETRY_EVERY = timedelta(minutes=1)  # a stock without a quote yet is looked at again this often, until the close
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


def check_day(week: date) -> date | None:
    """The check day of the week starting Monday `week`: its Friday, or the
    nearest trading day before it in the week (an NSE holiday); None for a
    week without trading."""
    return next((day for day in (week + timedelta(days=k) for k in (4, 3, 2, 1, 0)) if _is_trading_day(day)), None)


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


async def _days(ctx, ids: list[uuid.UUID], start: date, end: date) -> dict[uuid.UUID, list[dict]]:
    """Each instrument's stored daily bars from `start` to before `end`,
    oldest first -- one query for all."""
    if not ids:
        return {}
    rows = await ctx.db.execute(
        select(OhlcvCandle.instrument_id, OhlcvCandle.ts, OhlcvCandle.open, OhlcvCandle.high, OhlcvCandle.low, OhlcvCandle.close)
        .where(OhlcvCandle.instrument_id.in_(ids), OhlcvCandle.timeframe == STORED,
               OhlcvCandle.ts >= _midnight(start), OhlcvCandle.ts < _midnight(end))
        .order_by(OhlcvCandle.instrument_id, OhlcvCandle.ts)
    )
    days: dict[uuid.UUID, list[dict]] = {}
    for instrument_id, ts, o, h, lo, c in rows:
        day = as_aware_utc(ts).astimezone(IST).date()
        bars = days.setdefault(instrument_id, [])
        if None in (o, h, lo, c) or (bars and bars[-1]["day"] == day):
            continue  # one candle a day
        bars.append({"day": day, "open": float(o), "high": float(h), "low": float(lo), "close": float(c)})
    return days


async def _today(ctx, instruments: list[Instrument], today: date) -> tuple[dict[uuid.UUID, dict], str | None]:
    """Today's bar so far by instrument -- Kite's quote: today's open, high
    and low, the last price as its close -- and why there are none. In a
    backtest, the stored day."""
    if ctx.is_backtest:
        stored = await _days(ctx, [i.id for i in instruments], today, today + timedelta(days=1))
        return {i: bars[-1] for i, bars in stored.items() if bars}, None
    creds = await find_connected_zerodha_credentials(ctx.db)
    if creds is None:
        return {}, "Zerodha isn't logged in"
    broker = ZerodhaKiteBroker()
    broker._api_key, broker._access_token = creds["api_key"], creds["access_token"]
    keys = {f"{i.exchange}:{i.external_ref}": i.id for i in instruments}
    quotes, error = await kite_quotes(broker, list(keys))
    out: dict[uuid.UUID, dict] = {}
    for key, instrument_id in keys.items():
        q = quotes.get(key) or {}
        ohlc = q.get("ohlc") or {}
        price, o, h, lo = q.get("last_price"), ohlc.get("open"), ohlc.get("high"), ohlc.get("low")
        if not price or not o or not h or not lo:
            continue
        price = float(price)
        out[instrument_id] = {"day": today, "open": float(o), "high": max(float(h), price), "low": min(float(lo), price), "close": price}
    return out, (None if out else f"no Kite quotes ({error or 'none returned'})")


def week_so_far(days: list[dict], today_bar: dict | None, this_week: date, today: date) -> list[dict] | None:
    """The stock's weekly bars with this week's as of now last; None when
    a day is missing: last week's last trading day, one of this week's
    before today, or today's quote."""
    before = [d for d in days if d["day"] < this_week]
    during = [d for d in days if this_week <= d["day"] < today]
    finished = weeks(before)
    if not finished or finished[-1]["week"] != this_week - timedelta(weeks=1) or finished[-1]["last_day"] != last_trading_day_before(this_week):
        return None
    expected = [this_week + timedelta(days=k) for k in range((today - this_week).days) if _is_trading_day(this_week + timedelta(days=k))]
    if [d["day"] for d in during] != expected or today_bar is None:
        return None
    return finished + weeks(during + [today_bar])


def _next_check(now_ist: datetime) -> str:
    week = monday(now_ist.date())
    while True:
        day = check_day(week)
        if day is not None and (day > now_ist.date() or (day == now_ist.date() and now_ist.time() < DECIDE_AT)):
            return f"{day:%a %d-%b} {DECIDE_AT:%H:%M}"
        week += timedelta(weeks=1)


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
    notes: list[str] = []

    # --- Stop -> "Exit positions": every holding, at live prices.
    if force_exit:
        sold = []
        for symbol, leg in list(holdings.items()):
            price = await ctx.get_price(uuid.UUID(leg["instrument_id"]))
            if price is None:
                notes.append(f"{symbol}: no live price yet")
                continue
            holdings.pop(symbol)
            pnl = await _sell(ctx, symbol, leg, price, "manual")
            sold.append(f"{symbol} @ {price:,.2f} (P&L {pnl:+,.0f})")
        ctx.state["holdings"] = holdings
        ctx.note("exited", signal="COVER", reason=f"manual exit: sold {len(sold)}" + (f" | {'; '.join(notes)}" if notes else ""))
        return

    universe = {
        i.symbol: i for i in (
            await ctx.db.execute(select(Instrument).where(Instrument.exchange == "NSE", Instrument.symbol.in_(STOCK_UNIVERSE)))
        ).scalars().all()
    }
    held = {symbol: await ctx.db.get(Instrument, uuid.UUID(leg["instrument_id"])) for symbol, leg in holdings.items()}

    # --- The week's check: Friday (or the day before an NSE holiday) at 15:00.
    day = check_day(this_week)
    decide_at = datetime.combine(today, DECIDE_AT, tzinfo=IST)
    if day == today and now_ist < decide_at:
        ctx.wake_at(decide_at)
    check = ctx.state.get("check") or {}
    if check.get("day") != today.isoformat():
        check = {"day": today.isoformat(), "decided": [], "waiting": None, "at": None}
    due = (
        day == today and market_open(now_ist) and now_ist >= decide_at and check["waiting"] != 0
        and (check["at"] is None or ctx.now - datetime.fromisoformat(check["at"]) >= RETRY_EVERY)
    )
    sold, bought, passed = [], [], []
    if due:
        check["at"] = ctx.now.isoformat()
        decided = set(check["decided"])
        look = {s: i for s, i in {**universe, **{s: i for s, i in held.items() if i is not None}}.items() if s not in decided}
        days = await _days(ctx, [i.id for i in look.values()], this_week - timedelta(weeks=HISTORY_WEEKS), today)
        today_bars, why_none = await _today(ctx, list(look.values()), today)
        if why_none:
            notes.append(why_none)
        bars_of = {s: week_so_far(days.get(i.id, []), today_bars.get(i.id), this_week, today) for s, i in look.items()}
        waiting = 0

        # Exits first: a holding bought before this check, its week closing under its 10-week MA.
        for symbol, leg in list(holdings.items()):
            if symbol in decided or held.get(symbol) is None:
                continue
            if datetime.fromisoformat(leg["opened_at"]) >= decide_at:
                decided.add(symbol)  # bought at this check: from the next one
                continue
            bars = bars_of.get(symbol)
            if bars is None:
                waiting += 1
                continue
            ma = moving_average(bars)
            decided.add(symbol)
            if ma is None or bars[-1]["close"] >= ma:
                continue
            price = bars[-1]["close"]
            holdings.pop(symbol)
            pnl = await _sell(ctx, symbol, leg, price, "ma_exit")
            sold.append(f"{symbol} @ {price:,.2f} (week to {today:%d-%b} at {DECIDE_AT:%H:%M}: {price:,.2f} < its 10-week MA {ma:,.2f}, "
                        f"P&L {pnl:+,.0f})")

        # Then this week's breakouts, strongest first, while a slot is free.
        found = []
        for symbol, instrument in universe.items():
            if symbol in holdings or symbol in decided:
                continue
            bars = bars_of.get(symbol)
            if bars is None:
                if len(weeks([d for d in days.get(instrument.id, []) if d["day"] < this_week])) < BREAKOUT_CANDLES:
                    decided.add(symbol)  # too few weeks to break out
                else:
                    waiting += 1
                continue
            strength = breakout(bars)
            if strength is None:
                decided.add(symbol)
            else:
                found.append((strength, symbol, bars[-1]["close"]))
        found.sort(key=lambda x: (-x[0], x[1]))
        equity = ctx.portfolio.cash
        for symbol, leg in holdings.items() if found else []:
            price = (bars_of.get(symbol) or [{}])[-1].get("close") or await ctx.get_price(uuid.UUID(leg["instrument_id"]))
            equity += (price or leg["entry_price"]) * leg["quantity"]
        for strength, symbol, price in found:
            decided.add(symbol)
            if len(holdings) >= MAX_POSITIONS:
                passed.append(symbol)
                continue
            quantity = float(int(min(equity / MAX_POSITIONS, ctx.portfolio.cash) / price))
            if quantity <= 0:
                passed.append(symbol)
                continue
            instrument = universe[symbol]
            await ctx.open_leg(instrument, "buy", quantity, price)
            holdings[symbol] = {
                "instrument_id": str(instrument.id), "quantity": quantity, "entry_price": price,
                "opened_at": ctx.now.isoformat(), "breakout_pct": round(strength, 2), "breakout_week": this_week.isoformat(),
            }
            bought.append(f"{symbol} @ {price:,.2f} (+{strength:.2f}% over its 52-week high)")
        check["decided"], check["waiting"] = sorted(decided), waiting
        if waiting:
            ctx.wake_at(ctx.now + RETRY_EVERY)
    ctx.state["check"] = check
    if check["waiting"] and check["day"] == today.isoformat() and market_open(now_ist):
        notes.append(f"{check['waiting']} stock(s) without today's quote or a stored day: looked at again every minute until the close")

    ctx.state["holdings"] = holdings
    text = []
    if sold:
        text.append("sold " + "; ".join(sold))
    if bought:
        text.append("bought " + "; ".join(bought))
    if passed:
        text.append(f"no slot for {len(passed)} more breakout(s): {', '.join(passed[:5])}{'...' if len(passed) > 5 else ''}")
    text.extend(notes)
    text.append(f"holding {len(holdings)}/{MAX_POSITIONS} | {len(universe)} stocks watched | next check {_next_check(now_ist)}")
    action = "entered" if bought else "exited" if sold else "hold" if holdings else "skipped"
    signal = "BUY" if bought else "SELL" if sold else None
    ctx.note(action, signal=signal, reason=" | ".join(text))
