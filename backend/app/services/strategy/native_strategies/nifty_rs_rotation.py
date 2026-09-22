"""
Nifty RS Rotation -- Cross-Sectional Relative Strength Rotation Strategy (native, unsandboxed)
=================================================================================================

Ported from an AmiBroker AFL relative-strength ranking scan. Ranks a fixed
universe of NSE stocks by their strength relative to NIFTY 50, buys the
top TOP_N ranked names, and sells anything currently held that falls out
of the top TOP_N -- a rotating basket, rebalanced once per week.

  ratio  = stock_close / NIFTY_close, per bar
  values = ( ratio / (Sum(ratio, RS_WINDOW) / RS_SUM_DIVISOR) - 1 ) * RS_MULTIPLIER

RS_WINDOW=10, RS_SUM_DIVISOR=13, RS_MULTIPLIER=12 are the AFL's own numbers,
kept verbatim. Note RS_SUM_DIVISOR and RS_MULTIPLIER only rescale the
reported value -- both are constant across every stock each week, so
neither can ever change the ranking or which names get traded. RS_WINDOW
is the only one of the three that actually moves the rotation (verified
via a real backtest sweep over production candle data, RS_WINDOW=11 came
out ahead of the AFL's default 10 on both total return and drawdown,
RS_WINDOW=5 on Sharpe -- default left at 10 here pending a decision on
which to adopt).

TIMEFRAME = "1wk" here has no direct backing in ohlcv_candles at all --
Kite has no native weekly interval, and nothing in this pipeline persists
a resampled weekly bar back into storage (only a read-time chart-display
resample exists, app/services/market_data/resample.py, which this
strategy now calls directly). _recent_closes() below fetches real stored
"1d" candles and resamples them into weekly bars itself, using that same
app function, rather than querying ohlcv_candles for a "1wk" row that
will never exist. Verified against a standalone backtest harness that did
this same resampling externally -- see that verification for the
numbers this produced over 2021-2026 on both a 50-stock and the full
NSE 500 universe.

Rebalance timing: fires at/after 15:10 IST on Friday -- close enough to
the 15:30 close that the week's RS ranking reflects essentially the full
week's move, with slack before it for the scheduler's ~10s poll interval.
If Friday is an NSE trading holiday, the window shifts back a day at a
time (Thursday, then Wednesday, ...) onto the nearest earlier trading day
that week, via nse_holidays.is_trading_holiday -- the same holiday
calendar the market-hours gate already uses. Once a week's rebalance has
fired, evaluate() holds for the rest of that ISO week regardless of how
many ticks land after the window; if the strategy is offline through the
whole window (holiday outage, redeploy, etc.), the first tick once it's
back online fires the catch-up rebalance immediately rather than skipping
the week.

Because a weekly rebalance has no reason to re-scan the whole universe
every ~10-second evaluation tick (the scheduler calls evaluate()
unconditionally that often during market hours, see
paper_trading/scheduler.py), evaluate() below only does the real
ranking/rebalance work once per ISO week -- at that Friday (or
holiday-shifted) 15:10 IST window -- and just holds otherwise. Without
this, a 500-stock universe would fire ~501 DB queries plus 500 pandas
resamples every single tick, all day, for an answer that only actually
changes once a week.

Position sizing: POSITION_SIZE_PCT (default 10%, i.e. TOP_N=10 positions
fully invests tracked equity) of this strategy's own tracked equity --
cash plus the mark-to-market value of whatever it's currently holding --
per new entry. Deliberately NOT the whole pool's equity across every
other deployment sharing the same portfolio.

Exit rotation is deliberately decoupled from whether every single
STOCK_UNIVERSE symbol currently resolves to an Instrument row: a handful
of unresolvable symbols (renamed, delisted, not yet backfilled) only
drops those names out of the ranking pool, it no longer aborts the whole
evaluate() before the exit loop ever runs -- a single bad symbol used to
be able to freeze rotation (both entries and exits) indefinitely, since
the old all-or-nothing check ran before holdings were even looked at.
Exits are still looked up by the instrument_id stored on the leg (not
`universe`), so a position remains sellable even after its symbol is
edited out of STOCK_UNIVERSE entirely or fails to resolve that week.

STOCK_UNIVERSE, BENCHMARK_SYMBOL and TIMEFRAME are plain constants below.

Runs through services/paper_trading/native_runner.py -- same ctx-based
contract as the other native strategies in this codebase (ctx.state,
ctx.db, ctx.now, ctx.portfolio, ctx.get_price, ctx.open_leg, ctx.close_leg,
ctx.record_trade, ctx.note).
"""

import uuid
from datetime import date, datetime, time, timedelta

from sqlalchemy import select

from app.models.instrument import Instrument
from app.models.market_data import OhlcvCandle
from app.services.broker.zerodha_broker import IST
from app.services.market_data.nse_holidays import is_trading_holiday
from app.services.market_data.resample import resample_candles

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
    'HDFCLIFE', 'HEG', 'HEROMOTOCO', 'HEXT', 'HFCL', 'HINDALCO', 'HINDCOPPER', 'HINDPETRO',
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

TIMEFRAME = "1wk"

TOP_N = 10
POSITION_SIZE_PCT = 10.0

RS_WINDOW = 10
RS_SUM_DIVISOR = 13.0
RS_MULTIPLIER = 12.0
RS_HISTORY_BARS = RS_WINDOW + 5

# How many calendar days of raw daily candles to pull per instrument before
# resampling to weekly -- enough to comfortably cover RS_HISTORY_BARS closed
# weekly bars (roughly *7, plus slack for holidays/weekends already baked
# into a week) without fetching an instrument's entire multi-year history
# on every rebalance.
_DAILY_LOOKBACK_DAYS = (RS_HISTORY_BARS + 4) * 7

REBALANCE_TIME = time(15, 10)
REBALANCE_WEEKDAY = 4  # date.weekday(): Mon=0 ... Fri=4 ... Sun=6


def _rebalance_date_for_week(ist_date: date) -> date:
    """Friday of ist_date's ISO week, shifted back a day at a time onto the
    nearest earlier trading day whenever it lands on an NSE holiday. Stops
    at Monday even if that's a holiday too -- nse_market_open() already
    keeps evaluate() from ever running on a day the market isn't open, so
    a whole-week holiday just pushes the actual first qualifying tick to
    whenever the market next reopens, rather than looping into the
    previous week."""
    friday = ist_date + timedelta(days=REBALANCE_WEEKDAY - ist_date.weekday())
    candidate = friday
    while candidate.weekday() > 0 and is_trading_holiday(candidate):
        candidate -= timedelta(days=1)
    return candidate


async def _recent_closes(ctx, instrument_id, limit: int) -> list[float]:
    cutoff = ctx.now - timedelta(days=_DAILY_LOOKBACK_DAYS)
    result = await ctx.db.execute(
        select(OhlcvCandle.ts, OhlcvCandle.open, OhlcvCandle.high, OhlcvCandle.low, OhlcvCandle.close, OhlcvCandle.volume)
        .where(OhlcvCandle.instrument_id == instrument_id, OhlcvCandle.timeframe == "1d", OhlcvCandle.ts >= cutoff)
        .order_by(OhlcvCandle.ts.asc())
    )
    daily_bars = [
        {"ts": r.ts, "open": r.open, "high": r.high, "low": r.low, "close": r.close, "volume": r.volume}
        for r in result.all()
    ]
    weekly_bars = resample_candles(daily_bars, "1wk", only_closed=True, now=ctx.now)
    return [b["close"] for b in weekly_bars[-limit:]]


def _latest_rs_value(stock_closes: list[float], bench_closes: list[float]) -> float | None:
    n = min(len(stock_closes), len(bench_closes))
    if n < RS_WINDOW:
        return None
    stock_closes, bench_closes = stock_closes[-n:], bench_closes[-n:]
    ratios = [s / b for s, b in zip(stock_closes, bench_closes) if b]
    if len(ratios) < RS_WINDOW:
        return None
    latest_ratio = ratios[-1]
    rolling_avg = sum(ratios[-RS_WINDOW:]) / RS_SUM_DIVISOR
    if rolling_avg == 0:
        return None
    return (latest_ratio / rolling_avg - 1) * RS_MULTIPLIER


async def evaluate(ctx) -> None:
    now_ist = ctx.now.astimezone(IST)
    current_week_key = now_ist.strftime("%G-W%V")  # ISO year-week
    if ctx.state.get("last_rebalance_period") == current_week_key:
        ctx.note("hold", reason=f"already rebalanced for {current_week_key} -- holding {len(ctx.state.get('holdings', {}))}/{TOP_N}")
        return

    target_date = _rebalance_date_for_week(now_ist.date())
    target_dt = datetime.combine(target_date, REBALANCE_TIME, tzinfo=IST)
    if now_ist < target_dt:
        ctx.note(
            "hold",
            reason=f"waiting for this week's rebalance window ({target_date.isoformat()} {REBALANCE_TIME.isoformat()} IST)"
                   f" -- holding {len(ctx.state.get('holdings', {}))}/{TOP_N}",
        )
        return

    benchmark = (
        await ctx.db.execute(select(Instrument).where(Instrument.symbol == BENCHMARK_SYMBOL))
    ).scalar_one_or_none()
    if benchmark is None:
        ctx.note("skipped", reason=f"{BENCHMARK_SYMBOL} instrument not found")
        return

    universe_result = await ctx.db.execute(
        select(Instrument).where(Instrument.exchange == "NSE", Instrument.symbol.in_(STOCK_UNIVERSE))
    )
    universe = {row.symbol: row for row in universe_result.scalars().all()}
    missing = [s for s in STOCK_UNIVERSE if s not in universe]
    if missing:
        # Deliberately not returning here -- a handful of unresolvable
        # symbols should only shrink the ranking pool, not freeze the
        # whole rotation (entries and exits both) the way an early return
        # would. See module docstring.
        ctx.note("note", reason=f"{len(missing)} universe symbol(s) not found, ranking without them: {', '.join(missing)}")

    bench_closes = await _recent_closes(ctx, benchmark.id, RS_HISTORY_BARS)
    if len(bench_closes) < RS_WINDOW:
        ctx.note("skipped", reason=f"not enough {BENCHMARK_SYMBOL} history yet on {TIMEFRAME}")
        return

    scores: dict[str, float] = {}
    for symbol, instrument in universe.items():
        closes = await _recent_closes(ctx, instrument.id, RS_HISTORY_BARS)
        value = _latest_rs_value(closes, bench_closes)
        if value is not None:
            scores[symbol] = value

    if not scores:
        ctx.state["last_rebalance_period"] = current_week_key
        ctx.note("skipped", reason="no RS scores could be computed yet (insufficient candle history)")
        return

    ranked = sorted(scores.items(), key=lambda kv: kv[1], reverse=True)
    top_symbols = {symbol for symbol, _ in ranked[:TOP_N]}

    holdings = ctx.state.get("holdings", {})  # {symbol: {instrument_id, quantity, entry_price, opened_at}}

    # --- Exits: anything held that fell out of the top TOP_N this tick. ---
    # Looked up by the instrument_id stored on the leg, not `universe`, so a
    # position stays sellable even after its symbol drops out of
    # STOCK_UNIVERSE or fails to resolve above.
    sold, bought, stuck = [], [], []
    for symbol in list(holdings.keys()):
        if symbol in top_symbols:
            continue
        leg = holdings[symbol]
        instrument = await ctx.db.get(Instrument, uuid.UUID(leg["instrument_id"]))
        if instrument is None:
            stuck.append(symbol)  # nothing to close against -- retry next rebalance
            continue
        holdings.pop(symbol)
        price = await ctx.get_price(instrument.id)
        if price is None or price <= 0:
            price = leg["entry_price"]
        await ctx.close_leg(instrument, "sell", leg["quantity"], price)
        pnl = (price - leg["entry_price"]) * leg["quantity"]
        pnl_pct = (pnl / (leg["entry_price"] * leg["quantity"]) * 100) if leg["entry_price"] else 0.0
        await ctx.record_trade(
            legs=[{
                "instrument_id": leg["instrument_id"], "side": "long", "quantity": leg["quantity"],
                "entry_price": leg["entry_price"], "exit_price": price,
            }],
            pnl=pnl, pnl_pct=pnl_pct, exit_reason="dropped_out_of_top_n",
            opened_at=datetime.fromisoformat(leg["opened_at"]),
        )
        sold.append(symbol)

    # --- Equity available for new entries: cash plus mark-to-market of
    # whatever this strategy is still holding after the exits above. ------
    equity = ctx.portfolio.cash
    for leg in holdings.values():
        price = (await ctx.get_price(uuid.UUID(leg["instrument_id"]))) or leg["entry_price"]
        equity += price * leg["quantity"]

    # --- Entries: top-ranked symbols not already held. ---------------------
    for symbol in [s for s, _ in ranked[:TOP_N] if s not in holdings]:
        instrument = universe.get(symbol)
        if instrument is None:
            continue
        price = await ctx.get_price(instrument.id)
        if price is None or price <= 0:
            continue
        allocation = equity * (POSITION_SIZE_PCT / 100)
        lot_size = instrument.lot_size or 1
        quantity = float(int(allocation / price / lot_size) * lot_size)
        if quantity <= 0:
            continue
        await ctx.open_leg(instrument, "buy", quantity, price)
        holdings[symbol] = {
            "instrument_id": str(instrument.id), "quantity": quantity, "entry_price": price,
            "opened_at": ctx.now.isoformat(), "rs_value": scores[symbol],
        }
        equity -= quantity * price
        bought.append(symbol)

    ctx.state["holdings"] = holdings
    ctx.state["last_rebalance_period"] = current_week_key

    if bought or sold:
        reason = f"bought: {', '.join(bought) or 'none'} | sold: {', '.join(sold) or 'none'} | holding {len(holdings)}/{TOP_N}"
        if stuck:
            reason += f" | stuck (instrument missing, retry next week): {', '.join(stuck)}"
        ctx.note("entered" if bought else "exited", signal="REBALANCE", reason=reason)
    else:
        ctx.note("hold", reason=f"holding {len(holdings)}/{TOP_N}: {', '.join(sorted(holdings)) or 'none'}")
