"""
Nifty RS Rotation -- Cross-Sectional Relative Strength Rotation Strategy (native, unsandboxed)
=================================================================================================

Ported from an AmiBroker AFL relative-strength ranking scan. Ranks a fixed
universe of NSE stocks by their strength relative to NIFTY 50 on weekly
closes and holds the top TOP_N, equally weighted -- a rotating basket,
readjusted once a week.

  ratio  = stock_close / NIFTY_close, per weekly bar
  values = ( ratio / (Sum(ratio, RS_WINDOW) / RS_SUM_DIVISOR) - 1 ) * RS_MULTIPLIER

RS_WINDOW=10, RS_SUM_DIVISOR=13, RS_MULTIPLIER=12 are the AFL's own numbers,
kept verbatim. RS_SUM_DIVISOR and RS_MULTIPLIER only rescale the value --
the same for every stock -- so neither changes the ranking; RS_WINDOW is
the one that moves the rotation.

When it trades (agreed 6 Oct):
  - On the first check after it's started: ranks and buys the top TOP_N
    straight away, rather than waiting for Friday. In a backtest that's
    09:15 on the start date, at that day's opening price.
  - Every Friday at 15:00 IST: readjusts. Sells what fell out of the top
    TOP_N (or out of STOCK_UNIVERSE), then brings every holding to
    POSITION_SIZE_PCT of equity -- trims the ones above, tops up the ones
    below, buys the new entrants -- leaving any already within
    REWEIGHT_BAND_PCT points of it alone, so charges aren't paid on noise.
  - Friday an NSE holiday: the nearest earlier trading day, at 15:00.
    Live that's the NSE holiday calendar (nse_holidays.py, seeded from
    2026); a backtest also counts any weekday NIFTY 50 has no candle for
    as a holiday, so earlier years' Good Fridays and so on shift too.
  - Missed (offline at 15:00): the first check after it, the same week.

Weekly closes are built here from the stored daily candles (Kite has no
weekly interval) -- only days finished before today, so a backtest never
sees later prices -- grouped by IST week. The current week's close is the
price at the moment of the decision (15:00 on Friday, or whenever it
started): what AmiBroker's live scan does with a forming weekly bar. Live
that's the Kite price (ctx.get_prices); in a backtest it's the close of the
last 5-minute candle finished by then, else the day's daily candle -- its
open at 09:15, its close later in the day (stored 5-minute history for
these stocks only starts in July 2026, so before that Friday's close stands
in for the 15:00 price).

Equity for sizing is this strategy's own: cash plus its holdings at the
decision price -- never the rest of a shared pool.

state["force_exit"] (Stop with exit, and the end of a backtest) sells
everything held at the current price.

Runs through services/paper_trading/native_runner.py (ctx.state, ctx.db,
ctx.now, ctx.portfolio, ctx.get_prices, ctx.open_leg, ctx.close_leg,
ctx.record_trade, ctx.note, ctx.wake_at).
"""

import uuid
from datetime import date, datetime, time, timedelta, timezone

from sqlalchemy import func, select

from app.core.time import as_aware_utc
from app.models.instrument import Instrument
from app.models.market_data import OhlcvCandle
from app.services.broker.zerodha_broker import IST
from app.services.market_data.nse_holidays import is_trading_holiday

# --- Amend this list to change which stocks are tracked ------------------
STOCK_UNIVERSE = [
    '360ONE', '3MINDIA', 'AADHARHFC', 'AARTIIND', 'AAVAS', 'ABB', 'ABBOTINDIA', 'ABCAPITAL',
    'ABDL', 'ABFRL', 'ABLBL', 'ABREL', 'ABSLAMC', 'ACC', 'ACE', 'ACMESOLAR',
    'ACUTAAS', 'ADANIENSOL', 'ADANIENT', 'ADANIGREEN', 'ADANIPORTS', 'ADANIPOWER', 'AEGISLOG', 'AEGISVOPAK',
    'AFCONS', 'AFFLE', 'AIAENG', 'AIIL', 'AJANTPHARM', 'ALKEM', 'AMBER', 'AMBUJACEM',
    'ANANDRATHI', 'ANANTRAJ', 'ANGELONE', 'ANTHEM', 'ANURAS', 'APARINDS', 'APLAPOLLO', 'APOLLOHOSP',
    'APOLLOTYRE', 'APTUS', 'ARE&M', 'ASAHIINDIA', 'ASHOKLEY', 'ASIANPAINT', 'ASTERDM', 'ASTRAL',
    'ATGL', 'ATHERENERG', 'ATUL', 'AUBANK', 'AUROPHARMA', 'AWL', 'AXISBANK', 'BAJAJ-AUTO',
    'BAJAJFINSV', 'BAJAJHFL', 'BAJAJHLDNG', 'BAJFINANCE', 'BALKRISIND', 'BALRAMCHIN', 'BANDHANBNK', 'BANKBARODA',
    'BANKINDIA', 'BATAINDIA', 'BAYERCROP', 'BBTC', 'BDL', 'BEL', 'BELRISE', 'BEML',
    'BERGEPAINT', 'BHARATFORG', 'BHARTIARTL', 'BHARTIHEXA', 'BHEL', 'BIKAJI', 'BIOCON', 'BLS',
    'BLUEDART', 'BLUEJET', 'BLUESTARCO', 'BOSCHLTD', 'BPCL', 'BRIGADE', 'BRITANNIA', 'BSE',
    'BSOFT', 'CAMS', 'CANBK', 'CANFINHOME', 'CANHLIFE', 'CAPLIPOINT', 'CARBORUNIV', 'CARTRADE',
    'CASTROLIND', 'CCL', 'CDSL', 'CEATLTD', 'CEMPRO', 'CENTRALBK', 'CESC', 'CGCL',
    'CGPOWER', 'CHALET', 'CHAMBLFERT', 'CHENNPETRO', 'CHOICEIN', 'CHOLAFIN', 'CHOLAHLDNG', 'CIEINDIA',
    'CIPLA', 'CLEAN', 'COALINDIA', 'COCHINSHIP', 'COFORGE', 'COHANCE', 'COLPAL', 'CONCOR',
    'CONCORDBIO', 'COROMANDEL', 'CPPLUS', 'CRAFTSMAN', 'CREDITACC', 'CRISIL', 'CROMPTON', 'CUB',
    'CUMMINSIND', 'CYIENT', 'DABUR', 'DALBHARAT', 'DATAPATTNS', 'DCMSHRIRAM', 'DEEPAKFERT', 'DEEPAKNTR',
    'DELHIVERY', 'DEVYANI', 'DIVISLAB', 'DIXON', 'DLF', 'DMART', 'DOMS', 'DRREDDY',
    'ECLERX', 'EICHERMOT', 'EIDPARRY', 'EIHOTEL', 'ELECON', 'ELGIEQUIP', 'EMAMILTD', 'EMCURE',
    'EMMVEE', 'ENDURANCE', 'ENGINERSIN', 'ENRIN', 'ERIS', 'ESCORTS', 'ETERNAL', 'EXIDEIND',
    'FACT', 'FEDERALBNK', 'FINCABLES', 'FIRSTCRY', 'FIVESTAR', 'FLUOROCHEM', 'FORCEMOT', 'FORTIS',
    'FSL', 'GABRIEL', 'GAIL', 'GALLANTT', 'GESHIP', 'GICRE', 'GILLETTE', 'GLAND',
    'GLAXO', 'GLENMARK', 'GMDCLTD', 'GMRAIRPORT', 'GODFRYPHLP', 'GODIGIT', 'GODREJCP', 'GODREJIND',
    'GODREJPROP', 'GPIL', 'GRANULES', 'GRAPHITE', 'GRASIM', 'GRAVITA', 'GROWW', 'GRSE',
    'GVT&D', 'HAL', 'HAVELLS', 'HBLENGINE', 'HCLTECH', 'HDBFS', 'HDFCAMC', 'HDFCBANK',
    'HDFCLIFE', 'HEGAM', 'HEROMOTOCO', 'HEXT', 'HFCL', 'HINDALCO', 'HINDCOPPER', 'HINDPETRO',
    'HINDUNILVR', 'HINDZINC', 'HOMEFIRST', 'HONASA', 'HONAUT', 'HSCL', 'HUDCO', 'HYUNDAI',
    'ICICIAMC', 'ICICIBANK', 'ICICIGI', 'ICICIPRULI', 'IDBI', 'IDEA', 'IDFCFIRSTB', 'IEX',
    'IFCI', 'IGIL', 'IGL', 'IIFL', 'IKS', 'INDGN', 'INDHOTEL', 'INDIACEM',
    'INDIAMART', 'INDIANB', 'INDIGO', 'INDUSINDBK', 'INDUSTOWER', 'INFY', 'INOXWIND', 'INTELLECT',
    'IOB', 'IOC', 'IPCALAB', 'IRB', 'IRCON', 'IRCTC', 'IREDA', 'IRFC',
    'ITC', 'ITCHOTELS', 'ITI', 'J&KBANK', 'JAINREC', 'JBMA', 'JINDALSAW', 'JINDALSTEL',
    'JIOFIN', 'JKCEMENT', 'JKTYRE', 'JMFINANCIL', 'JPPOWER', 'JSL', 'JSWCEMENT', 'JSWDULUX',
    'JSWENERGY', 'JSWINFRA', 'JSWSTEEL', 'JUBLFOOD', 'JUBLINGREA', 'JUBLPHARMA', 'JWL', 'JYOTICNC',
    'KAJARIACER', 'KALYANKJIL', 'KARURVYSYA', 'KAYNES', 'KEC', 'KEI', 'KFINTECH', 'KIMS',
    'KIRLOSENG', 'KOTAKBANK', 'KPIL', 'KPITTECH', 'KPRMILL', 'LALPATHLAB', 'LATENTVIEW', 'LAURUSLABS',
    'LEMONTREE', 'LENSKART', 'LGEINDIA', 'LICHSGFIN', 'LICI', 'LINDEINDIA', 'LLOYDSME', 'LODHA',
    'LT', 'LTF', 'LTFOODS', 'LTM', 'LTTS', 'LUPIN', 'M&M', 'M&MFIN',
    'MAHABANK', 'MANAPPURAM', 'MANKIND', 'MAPMYINDIA', 'MARICO', 'MARUTI', 'MAXHEALTH', 'MAZDOCK',
    'MCX', 'MEDANTA', 'MEESHO', 'MFSL', 'MGL', 'MINDACORP', 'MMTC', 'MOTHERSON',
    'MOTILALOFS', 'MPHASIS', 'MRF', 'MRPL', 'MSUMI', 'MUTHOOTFIN', 'NAM-INDIA', 'NATCOPHARM',
    'NATIONALUM', 'NAUKRI', 'NAVA', 'NAVINFLUOR', 'NBCC', 'NCC', 'NESTLEIND', 'NETWEB',
    'NEULANDLAB', 'NEWGEN', 'NH', 'NHPC', 'NIACL', 'NIVABUPA', 'NLCINDIA', 'NMDC',
    'NSLNISP', 'NTPC', 'NTPCGREEN', 'NUVAMA', 'NUVOCO', 'NYKAA', 'OBEROIRLTY', 'OFSS',
    'OIL', 'OLAELEC', 'OLECTRA', 'ONESOURCE', 'ONGC', 'PAGEIND', 'PARADEEP', 'PATANJALI',
    'PAYTM', 'PCBL', 'PERSISTENT', 'PETRONET', 'PFC', 'PFIZER', 'PFOCUS', 'PGEL',
    'PHOENIXLTD', 'PIDILITIND', 'PIIND', 'PINELABS', 'PIRAMALFIN', 'PNB', 'PNBHOUSING', 'POLICYBZR',
    'POLYCAB', 'POLYMED', 'POONAWALLA', 'POWERGRID', 'POWERINDIA', 'PPLPHARMA', 'PREMIERENE', 'PRESTIGE',
    'PTCIL', 'PVRINOX', 'PWL', 'RADICO', 'RAILTEL', 'RAINBOW', 'RAMCOCEM', 'RBLBANK',
    'RECLTD', 'REDINGTON', 'RELIANCE', 'RHIM', 'RITES', 'RKFORGE', 'RPOWER', 'RRKABEL',
    'RVNL', 'SAGILITY', 'SAIL', 'SAILIFE', 'SAMMAANCAP', 'SAPPHIRE', 'SARDAEN', 'SAREGAMA',
    'SBFC', 'SBICARD', 'SBILIFE', 'SBIN', 'SCHAEFFLER', 'SCHNEIDER', 'SCI', 'SHREECEM',
    'SHRIRAMFIN', 'SHYAMMETL', 'SIEMENS', 'SIGNATURE', 'SJVN', 'SOBHA', 'SOLARINDS', 'SONACOMS',
    'SONATSOFTW', 'SPLPETRO', 'SRF', 'STARHEALTH', 'SUMICHEM', 'SUNDARMFIN', 'SUNPHARMA', 'SUNTV',
    'SUPREMEIND', 'SUZLON', 'SWANCORP', 'SWIGGY', 'SYNGENE', 'SYRMA', 'TARIL', 'TATACAP',
    'TATACHEM', 'TATACOMM', 'TATACONSUM', 'TATAELXSI', 'TATAINVEST', 'TATAPOWER', 'TATASTEEL', 'TATATECH',
    'TBOTEK', 'TCS', 'TECHM', 'TECHNOE', 'TEGA', 'TEJASNET', 'TENNIND', 'THELEELA',
    'THERMAX', 'TIINDIA', 'TIMKEN', 'TITAGARH', 'TITAN', 'TMCV', 'TMPV', 'TORNTPHARM',
    'TORNTPOWER', 'TRAVELFOOD', 'TRENT', 'TRIDENT', 'TRITURBINE', 'TTML', 'TVSMOTOR', 'UBL',
    'UCOBANK', 'ULTRACEMCO', 'UNIONBANK', 'UNITDSPR', 'UNOMINDA', 'UPL', 'URBANCO', 'USHAMART',
    'UTIAMC', 'VBL', 'VEDL', 'VIJAYA', 'VMM', 'VOLTAS', 'VTL', 'WAAREEENER',
    'WELCORP', 'WELSPUNLIV', 'WHIRLPOOL', 'WIPRO', 'WOCKPHARMA', 'YESBANK', 'ZEEL', 'ZENSARTECH',
    'ZENTEC', 'ZFCVINDIA', 'ZYDUSLIFE', 'ZYDUSWELL',
]
BENCHMARK_SYMBOL = "NIFTY 50"

TIMEFRAME = "1wk"  # built from the stored daily candles, see the docstring

TOP_N = 10
POSITION_SIZE_PCT = 10.0  # of this strategy's equity, per stock
REWEIGHT_BAND_PCT = 1.0  # a holding at 9%-11% of equity is left as it is

RS_WINDOW = 10
RS_SUM_DIVISOR = 13.0
RS_MULTIPLIER = 12.0
DAILY_LOOKBACK = timedelta(days=(RS_WINDOW + 6) * 7)  # daily candles read: RS_WINDOW weeks and slack for holidays

REBALANCE_TIME = time(15, 0)
REBALANCE_WEEKDAY = 4  # date.weekday(): Mon=0 ... Fri=4
WAKE_AFTER = timedelta(seconds=3)

# Backtest prices: the last intraday candle finished by the moment.
INTRADAY = (("5m", timedelta(minutes=5)), ("15m", timedelta(minutes=15)))
FIRST_INTRADAY_CLOSE = time(9, 20)  # before it, a day's own candle can only give its open


def _midnight(d: date) -> datetime:
    return datetime.combine(d, time(0), tzinfo=IST).astimezone(timezone.utc)


def _week(d: date) -> tuple[int, int]:
    year, week, _ = d.isocalendar()
    return year, week


def _week_key(d: date) -> str:
    return "%d-W%02d" % _week(d)


async def _benchmark_has_candles(ctx, benchmark_id: uuid.UUID, d: date) -> bool:
    return bool(await ctx.db.scalar(
        select(func.count()).select_from(OhlcvCandle).where(
            OhlcvCandle.instrument_id == benchmark_id, OhlcvCandle.timeframe.in_(("1d", "5m", "15m")),
            OhlcvCandle.ts >= _midnight(d), OhlcvCandle.ts < _midnight(d + timedelta(days=1)),
        )
    ))


async def _is_session(ctx, benchmark_id: uuid.UUID, d: date) -> bool:
    """NSE trades on `d`: a weekday off the holiday calendar -- and, in a
    backtest, one NIFTY 50 has candles for, up to the last day it has any
    (the calendar only knows the years seeded; past those, the data does)."""
    if d.weekday() >= 5 or is_trading_holiday(d):
        return False
    if not ctx.is_backtest:
        return True
    last = await ctx.db.scalar(
        select(func.max(OhlcvCandle.ts)).where(OhlcvCandle.instrument_id == benchmark_id, OhlcvCandle.timeframe == "1d")
    )
    if last is None or d > as_aware_utc(last).astimezone(IST).date():
        return True
    return await _benchmark_has_candles(ctx, benchmark_id, d)


async def _rebalance_day(ctx, benchmark_id: uuid.UUID, today: date) -> date:
    """This week's Friday, or the nearest earlier trading day when it's a
    holiday (not past Monday)."""
    day = today + timedelta(days=REBALANCE_WEEKDAY - today.weekday())
    while day.weekday() > 0 and not await _is_session(ctx, benchmark_id, day):
        day -= timedelta(days=1)
    return day


async def _replayed_prices(ctx, ids: list[uuid.UUID]) -> dict[uuid.UUID, float]:
    """Backtest prices at ctx.now from candles finished by then -- see the
    module docstring."""
    now = ctx.now
    now_ist = now.astimezone(IST)
    day_start = _midnight(now_ist.date())
    prices: dict[uuid.UUID, float] = {}
    for timeframe, length in INTRADAY:
        missing = [i for i in ids if i not in prices]
        if not missing:
            break
        newest = (
            select(OhlcvCandle.instrument_id, func.max(OhlcvCandle.ts).label("ts"))
            .where(OhlcvCandle.instrument_id.in_(missing), OhlcvCandle.timeframe == timeframe,
                   OhlcvCandle.ts >= day_start, OhlcvCandle.ts <= now - length)
            .group_by(OhlcvCandle.instrument_id).subquery()
        )
        rows = await ctx.db.execute(
            select(OhlcvCandle.instrument_id, OhlcvCandle.close)
            .join(newest, (OhlcvCandle.instrument_id == newest.c.instrument_id) & (OhlcvCandle.ts == newest.c.ts))
            .where(OhlcvCandle.timeframe == timeframe)
        )
        prices.update({instrument_id: close for instrument_id, close in rows})
    missing = [i for i in ids if i not in prices]
    if missing:
        newest = (
            select(OhlcvCandle.instrument_id, func.max(OhlcvCandle.ts).label("ts"))
            .where(OhlcvCandle.instrument_id.in_(missing), OhlcvCandle.timeframe == "1d", OhlcvCandle.ts < day_start + timedelta(days=1))
            .group_by(OhlcvCandle.instrument_id).subquery()
        )
        rows = await ctx.db.execute(
            select(OhlcvCandle.instrument_id, OhlcvCandle.ts, OhlcvCandle.open, OhlcvCandle.close)
            .join(newest, (OhlcvCandle.instrument_id == newest.c.instrument_id) & (OhlcvCandle.ts == newest.c.ts))
            .where(OhlcvCandle.timeframe == "1d")
        )
        for instrument_id, ts, open_, close in rows:
            today = as_aware_utc(ts).astimezone(IST).date() == now_ist.date()
            prices[instrument_id] = open_ if today and now_ist.time() < FIRST_INTRADAY_CLOSE else close
    return prices


async def _prices_now(ctx, ids: list[uuid.UUID]) -> dict[uuid.UUID, float]:
    ids = list(dict.fromkeys(ids))
    return await _replayed_prices(ctx, ids) if ctx.is_backtest else await ctx.get_prices(ids)


async def _weekly_closes(ctx, ids: list[uuid.UUID], today: date) -> dict[uuid.UUID, dict[tuple[int, int], float]]:
    """{instrument: {(ISO year, week): its last daily close}} from the days
    finished before `today`."""
    rows = await ctx.db.execute(
        select(OhlcvCandle.instrument_id, OhlcvCandle.ts, OhlcvCandle.close)
        .where(OhlcvCandle.instrument_id.in_(ids), OhlcvCandle.timeframe == "1d",
               OhlcvCandle.ts >= _midnight(today) - DAILY_LOOKBACK, OhlcvCandle.ts < _midnight(today))
        .order_by(OhlcvCandle.ts)
    )
    weekly: dict[uuid.UUID, dict[tuple[int, int], float]] = {i: {} for i in ids}
    for instrument_id, ts, close in rows:
        weekly[instrument_id][_week(as_aware_utc(ts).astimezone(IST).date())] = close
    return weekly


def rs_value(stock: list, bench: list) -> float | None:
    """The AFL value on the last RS_WINDOW weekly closes -- None unless every
    one of them has both closes."""
    if len(stock) < RS_WINDOW or len(bench) < RS_WINDOW:
        return None
    pairs = list(zip(stock[-RS_WINDOW:], bench[-RS_WINDOW:]))
    if any(s is None or not b for s, b in pairs):
        return None
    ratios = [s / b for s, b in pairs]
    rolling = sum(ratios) / RS_SUM_DIVISOR
    return (ratios[-1] / rolling - 1) * RS_MULTIPLIER if rolling else None


def _series(closes: dict[tuple[int, int], float], weeks: list[tuple[int, int]]) -> list[float | None]:
    """A stock's close for each of `weeks`; a week it didn't trade in keeps
    its close before it."""
    out, last = [], None
    earlier = sorted(w for w in closes if w < weeks[0])
    if earlier:
        last = closes[earlier[-1]]
    for week in weeks:
        last = closes.get(week, last)
        out.append(last)
    return out


async def _close(ctx, symbol: str, leg: dict, quantity: float, price: float, reason: str) -> None:
    instrument = await ctx.db.get(Instrument, uuid.UUID(leg["instrument_id"]))
    await ctx.close_leg(instrument, "sell", quantity, price)
    pnl = (price - leg["entry_price"]) * quantity
    await ctx.record_trade(
        legs=[{
            "instrument_id": leg["instrument_id"], "side": "long", "quantity": quantity,
            "entry_price": leg["entry_price"], "exit_price": price,
        }],
        pnl=pnl, pnl_pct=(pnl / (leg["entry_price"] * quantity) * 100) if leg["entry_price"] else 0.0,
        exit_reason=reason, opened_at=datetime.fromisoformat(leg["opened_at"]),
    )


async def _readjust(ctx, benchmark: Instrument, label: str) -> tuple[str, str] | None:
    """Ranks and trades; (the action to note, its reason), or None when
    there's no NIFTY 50 price to rank against (tried again at the next check)."""
    today = ctx.now.astimezone(IST).date()
    holdings = ctx.state.get("holdings", {})  # {symbol: {instrument_id, quantity, entry_price, opened_at, rank, rs_value}}
    universe = {
        row.symbol: row for row in (
            await ctx.db.execute(select(Instrument).where(Instrument.exchange == "NSE", Instrument.symbol.in_(STOCK_UNIVERSE)))
        ).scalars()
    }
    held_ids = {symbol: uuid.UUID(leg["instrument_id"]) for symbol, leg in holdings.items()}
    prices = await _prices_now(ctx, [benchmark.id, *(i.id for i in universe.values()), *held_ids.values()])
    if not prices.get(benchmark.id):
        return None

    # --- Rank: finished weeks, plus this one closing at the price now. -----
    weekly = await _weekly_closes(ctx, [benchmark.id, *(i.id for i in universe.values())], today)
    for instrument_id, price in prices.items():
        if instrument_id in weekly:
            weekly[instrument_id][_week(today)] = price
    weeks = sorted(weekly[benchmark.id])[-RS_WINDOW:]
    bench = _series(weekly[benchmark.id], weeks)
    scores = {}
    for symbol, instrument in universe.items():
        if prices.get(instrument.id) and (value := rs_value(_series(weekly[instrument.id], weeks), bench)) is not None:
            scores[symbol] = value
    ranked = sorted(scores.items(), key=lambda kv: kv[1], reverse=True)
    rank = {symbol: i + 1 for i, (symbol, _) in enumerate(ranked)}
    top = [symbol for symbol, _ in ranked[:TOP_N]]

    # --- Sell what fell out of the top TOP_N (or out of the list). ---------
    sold, trimmed, added, bought, stuck = [], [], [], [], []
    for symbol in list(holdings):
        if symbol in top:
            continue
        leg, price = holdings[symbol], prices.get(held_ids[symbol])
        if not price or await ctx.db.get(Instrument, held_ids[symbol]) is None:
            stuck.append(symbol)  # nothing to sell against now -- tried again next week
            continue
        await _close(ctx, symbol, leg, leg["quantity"], price, "dropped_out_of_top_n" if symbol in universe else "left_universe")
        holdings.pop(symbol)
        sold.append(f"{symbol} ({'rank ' + str(rank[symbol]) if symbol in rank else 'not ranked'})")

    # --- Equal weights: trim, then top up and buy, highest ranked first. ---
    equity = ctx.portfolio.cash + sum(
        (prices.get(held_ids[symbol]) or leg["entry_price"]) * leg["quantity"] for symbol, leg in holdings.items()
    )
    target = equity * POSITION_SIZE_PCT / 100
    band = equity * REWEIGHT_BAND_PCT / 100
    for symbol in [s for s in top if s in holdings]:
        leg, price = holdings[symbol], prices[universe[symbol].id]
        excess = int((leg["quantity"] * price - target) / price)
        if leg["quantity"] * price - target > band and excess > 0:
            await _close(ctx, symbol, leg, float(excess), price, "trimmed_to_equal_weight")
            leg["quantity"] -= excess
            trimmed.append(symbol)
    for symbol in top:
        instrument, price = universe[symbol], prices[universe[symbol].id]
        leg = holdings.get(symbol)
        value = leg["quantity"] * price if leg else 0.0
        if leg and target - value <= band:
            continue
        quantity = float(int(min(target - value, ctx.portfolio.cash) / price))
        if quantity <= 0:
            continue
        await ctx.open_leg(instrument, "buy", quantity, price)
        if leg:
            leg["entry_price"] = (leg["entry_price"] * leg["quantity"] + price * quantity) / (leg["quantity"] + quantity)
            leg["quantity"] += quantity
            added.append(symbol)
        else:
            holdings[symbol] = {"instrument_id": str(instrument.id), "quantity": quantity, "entry_price": price,
                                "opened_at": ctx.now.isoformat()}
            bought.append(f"{symbol} (rank {rank[symbol]})")
    for symbol, leg in holdings.items():
        if symbol in rank:
            leg["rank"], leg["rs_value"] = rank[symbol], round(scores[symbol], 4)
    ctx.state["holdings"] = holdings

    parts = [f"{label}: bought {', '.join(bought) or 'none'}", f"sold {', '.join(sold) or 'none'}"]
    if trimmed or added:
        parts.append(f"reweighted {', '.join(trimmed + added)}")
    if stuck:
        parts.append(f"couldn't sell (no price): {', '.join(stuck)}")
    missing = len(STOCK_UNIVERSE) - len(universe)
    parts.append(f"holding {len(holdings)}/{TOP_N} | ranked {len(ranked)}/{len(STOCK_UNIVERSE)}" + (f" ({missing} not found)" if missing else ""))
    action = "entered" if bought or added else "exited" if sold or trimmed else "hold"
    return action, " | ".join(parts)


async def evaluate(ctx) -> None:
    now_ist = ctx.now.astimezone(IST)
    today = now_ist.date()
    holdings = ctx.state.get("holdings", {})
    benchmark = (
        await ctx.db.execute(select(Instrument).where(Instrument.symbol == BENCHMARK_SYMBOL, Instrument.exchange == "NSE"))
    ).scalars().first()
    if benchmark is None:
        ctx.note("skipped", reason=f"{BENCHMARK_SYMBOL} instrument not found")
        return

    # --- Stop with exit / the end of a backtest: sell everything. ---------
    if ctx.state.pop("force_exit", False):
        prices = await _prices_now(ctx, [uuid.UUID(leg["instrument_id"]) for leg in holdings.values()])
        reason = "backtest_end" if ctx.is_backtest else "manual_exit"
        for symbol, leg in list(holdings.items()):
            price = prices.get(uuid.UUID(leg["instrument_id"]))
            if price:
                await _close(ctx, symbol, leg, leg["quantity"], price, reason)
                holdings.pop(symbol)
        ctx.state["holdings"] = holdings
        ctx.note("exited", signal="EXIT", reason=f"sold everything held ({reason.replace('_', ' ')})"
                 + (f" -- no price for {', '.join(holdings)}" if holdings else ""))
        return

    week = _week_key(today)
    cached = ctx.state.get("_rebalance_day") or {}
    if cached.get("week") != week:
        cached = {"week": week, "day": (await _rebalance_day(ctx, benchmark.id, today)).isoformat()}
        ctx.state["_rebalance_day"] = cached
    rebalance_at = datetime.combine(date.fromisoformat(cached["day"]), REBALANCE_TIME, tzinfo=IST)
    due = now_ist >= rebalance_at

    # --- Started: buy the top TOP_N now (a deployment from before this rule
    # that already holds or has rebalanced carries on as it was). ---------
    if not ctx.state.get("started") and not holdings and not ctx.state.get("last_rebalance_period"):
        if not await _is_session(ctx, benchmark.id, today):
            ctx.note("hold", reason="market closed today -- buys at the next session")
            return
        done = await _readjust(ctx, benchmark, "started")
        if done is None:
            ctx.note("skipped", reason=f"no {BENCHMARK_SYMBOL} price yet -- trying again")
            return
        ctx.state["started"] = True
        if due:
            ctx.state["last_rebalance_period"] = week
        ctx.note(done[0], signal="REBALANCE", reason=done[1])
        return
    ctx.state["started"] = True

    if ctx.state.get("last_rebalance_period") == week or not due:
        if not due:
            ctx.wake_at(rebalance_at + WAKE_AFTER)
        when = f"{rebalance_at:%a %d %b %H:%M}" if not due else "next week"
        ctx.note("hold", reason=f"holding {len(holdings)}/{TOP_N} | next readjust {when}")
        return

    done = await _readjust(ctx, benchmark, f"readjusted {now_ist:%a %d %b %H:%M}")
    if done is None:
        ctx.note("skipped", reason=f"no {BENCHMARK_SYMBOL} price at the readjust yet -- trying again")
        return
    ctx.state["last_rebalance_period"] = week
    ctx.note(done[0], signal="REBALANCE", reason=done[1])
