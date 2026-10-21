"""Read-only: could Kotak Neo's market data replace Zerodha's? (21 Oct)

Run on the server inside the backend container by the health check
(input kotak_data_check), piped in on stdin. Places no order and saves
nothing. The log is public, so it prints only counts, NIFTY market data
and yes/no -- never a credential, an account detail or an error's text.

Checks, against Kotak's official SDK (kotakneoapi 3.0.7):
  1. whether the consumer key alone reads quotes from this server
  2. how far back Kotak's NIFTY history goes (15-min, 5-min, daily)
  3. today's NIFTY 15-min candles: Kotak against ours (from Zerodha)
  4. the NIFTY future today: Kotak against Zerodha, volume and OI too
  5. Kotak's option chain OI against our latest PCR record's
  6. a 50-contract quote: speed, and OI agreeing with the chain
  7. whether Kotak's candles carry OI (needed by the PCR gap fill)
"""

import asyncio
import json
import logging
import os
import statistics
import time
from datetime import date, datetime, timedelta, timezone

# Kotak's SDK logs each failed call -- address, parameters, the answer's
# text -- to stdout, and this log is public: keep it quiet (read on import).
os.environ["NEO_LOG_LEVEL"] = "CRITICAL"
os.environ["NEO_LOG_FILE_LEVEL"] = "CRITICAL"

import httpx  # noqa: E402
from sqlalchemy import select  # noqa: E402
from sqlalchemy.orm import selectinload  # noqa: E402

from app.core.encryption import decrypt_payload  # noqa: E402
from app.db.session import AsyncSessionLocal  # noqa: E402
from app.models.broker import Broker, BrokerAccount  # noqa: E402
from app.models.instrument import Instrument  # noqa: E402
from app.models.market_data import OhlcvCandle  # noqa: E402
from app.models.pcr import SOURCE_LIVE, PcrSnapshot, PcrStrikeOi  # noqa: E402
from app.services.broker.zerodha_broker import IST  # noqa: E402
from app.services.live_trading.broker_contracts import KIND_FUTURE, KIND_OPTION, kotak_rows  # noqa: E402
from app.services.options.pcr_snapshot_scheduler import kite_broker  # noqa: E402

PAUSE = 1.0  # between Kotak history calls: their rate limit isn't published (0.4 s was too fast)
BACKOFF = (2, 4, 8, 16)  # waits after a 429 "too many requests" before trying again
RATE_LIMITED = {"times": 0}
LAKH = 100_000.0


class section:
    """One numbered part of the check: a failure is reported and the rest still runs."""

    def __init__(self, title: str) -> None:
        print(f"\n{title}", flush=True)

    def __enter__(self) -> "section":
        return self

    def __exit__(self, kind, exc, tb) -> bool:
        if kind is not None and issubclass(kind, Exception):
            print(f"  this part failed ({kind.__name__})")
            return True
        return False


def failed(resp) -> str | None:
    """'error 400' etc. for a Kotak error answer -- the code only, never the text."""
    if isinstance(resp, dict):
        for key in ("fault", "error", "Error", "errors"):
            if key in resp:
                value = resp[key]
                code = value.get("code") if isinstance(value, dict) else None
                return f"error {code or resp.get('StatusCode') or ''}".strip()
        if str(resp.get("status", "")).lower() in ("error", "failed", "failure"):
            return f"error {resp.get('code') or ''}".strip()
    if resp is None:
        return "no answer"
    return None


def when(raw) -> datetime | None:
    if isinstance(raw, (int, float)) or (isinstance(raw, str) and raw.isdigit()):
        return datetime.fromtimestamp(int(raw), tz=timezone.utc)
    for fmt in ("%Y-%m-%dT%H:%M:%S%z", "%Y-%m-%d %H:%M:%S%z"):
        try:
            return datetime.strptime(str(raw), fmt)
        except ValueError:
            pass
    try:
        parsed = datetime.fromisoformat(str(raw))
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=IST)
    except ValueError:
        return None


def candles_of(resp) -> list[dict]:
    """Kotak's rows [ts, o, h, l, c, volume, oi] as dicts."""
    data = resp.get("data") if isinstance(resp, dict) else None
    rows = (data or {}).get("candles") if isinstance(data, dict) else data
    out = []
    for row in rows or []:
        if isinstance(row, (list, tuple)) and len(row) >= 5:
            out.append({"ts": when(row[0]), "open": float(row[1]), "high": float(row[2]), "low": float(row[3]),
                        "close": float(row[4]), "volume": row[5] if len(row) > 5 else None, "oi": row[6] if len(row) > 6 else None})
    return [c for c in out if c["ts"] is not None]


def history(client, neosymbol: str, interval: str, start: date, end: date) -> tuple[list[dict], str | None]:
    for wait in (*BACKOFF, None):
        resp = client.historical_data(neosymbol=neosymbol, interval=interval, from_date=start.isoformat(), to_date=end.isoformat())
        time.sleep(PAUSE)
        err = failed(resp)
        if err != "error 429" or wait is None:
            return candles_of(resp), err
        RATE_LIMITED["times"] += 1
        time.sleep(wait)
    return [], "error 429"


def shape(value, depth: int = 0, deepest: int = 2) -> str:
    """The answer's layout -- field names and types only, never values."""
    if isinstance(value, dict):
        if depth >= deepest:
            return "{" + ", ".join(sorted(value)[:12]) + "}"
        return "{" + ", ".join(f"{k}: {shape(v, depth + 1, deepest)}" for k, v in sorted(value.items())[:14]) + "}"
    if isinstance(value, list):
        return f"[{len(value)} x {shape(value[0], depth + 1, deepest)}]" if value else "[]"
    return type(value).__name__


def quote_rows(resp) -> list[dict]:
    rows = resp.get("data", resp) if isinstance(resp, dict) else resp
    return rows if isinstance(rows, list) else []


def fault_text(resp) -> str:
    """Kotak's own validation message for a refused call (no account detail is in these), cut short."""
    fault = resp.get("fault") if isinstance(resp, dict) else None
    if isinstance(fault, dict):
        return f"{fault.get('code')}: {str(fault.get('message') or fault.get('description') or '')[:90]}"
    return failed(resp) or "?"


def pick(row: dict, *names):
    return next((row[n] for n in names if isinstance(row, dict) and row.get(n) not in (None, "")), None)


def num(value) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def compare(name: str, ours: dict, theirs: dict, fields=("open", "high", "low", "close")) -> None:
    both = sorted(set(ours) & set(theirs))
    print(f"  {name}: {len(theirs)} Kotak, {len(ours)} ours, {len(both)} at the same times")
    if not both:
        return
    for field in fields:
        diffs = [abs(ours[t][field] - theirs[t][field]) for t in both if ours[t].get(field) is not None and theirs[t].get(field) is not None]
        if diffs:
            same = sum(d < 0.051 for d in diffs)
            print(f"    {field:6} identical {same}/{len(diffs)}, largest gap {max(diffs):,.2f}")


async def kotak_client(db):
    accounts = (await db.execute(
        select(BrokerAccount).join(Broker, Broker.id == BrokerAccount.broker_id)
        .options(selectinload(BrokerAccount.credential)).where(Broker.code == "kotak_neo")
    )).scalars().all()
    creds = next((json.loads(decrypt_payload(a.credential.encrypted_payload)) for a in accounts if a.credential), None)
    if not creds or not creds.get("consumer_key"):
        return None, None
    from neo_api_client import NeoAPI

    client = NeoAPI(environment="prod", access_token=None, neo_fin_key=None, consumer_key=creds["consumer_key"], http2=False)
    logging.getLogger("neo_api_client").setLevel(logging.CRITICAL)
    return client, creds


def log_in(client, creds) -> bool:
    import pyotp

    if not all(creds.get(k) for k in ("mobile_number", "ucc", "totp_secret", "mpin")):
        return False
    first = client.totp_login(mobile_number=creds["mobile_number"], ucc=creds["ucc"], totp=pyotp.TOTP(creds["totp_secret"]).now())
    return failed(first) is None and failed(client.totp_validate(mpin=creds["mpin"])) is None


async def main() -> None:
    today = datetime.now(IST).date()
    async with AsyncSessionLocal() as db:
        nifty_id = (await db.execute(select(Instrument.id).where(Instrument.symbol == "NIFTY 50"))).scalars().first()
        last = (await db.execute(select(OhlcvCandle.ts).where(OhlcvCandle.instrument_id == nifty_id, OhlcvCandle.timeframe == "15m")
                                 .order_by(OhlcvCandle.ts.desc()).limit(1))).scalars().first()
        session = as_ist(last).date() if last else today
        print(f"last session with our NIFTY candles: {session:%a %d %b}")
        client, creds = await kotak_client(db)
        print(f"Kotak account with a consumer key: {'yes' if client else 'no'}")
        if client is None:
            return
        kite = await kite_broker(db)
        print(f"Zerodha connected (for comparing): {'yes' if kite else 'no'}")

        # 1. Quotes with the consumer key alone; the NIFTY index's name/token.
        with section("1. Consumer key alone, from this server"):
            index = None
            logged_in = False
            for attempt in (1, 2):
                for token in ("Nifty 50", "NIFTY 50", "26000"):
                    started = time.monotonic()
                    resp = client.quotes(instrument_tokens=[{"instrument_token": token, "exchange_segment": "nse_cm"}], quote_type="ltp")
                    rows = [r for r in quote_rows(resp) if num(r.get("ltp"))]
                    if rows:
                        index = token
                        print(f"  NIFTY index quote: yes (as '{token}', {num(rows[0]['ltp']):,.2f}, {1000 * (time.monotonic() - started):.0f} ms)")
                        break
                if index or attempt == 2:
                    break
                print(f"  quote without a login: no ({failed(resp) or 'empty'}); logging in with TOTP to retry")
                logged_in = log_in(client, creds)
                print(f"  TOTP login: {'yes' if logged_in else 'no'}")
            if index is None:
                print("  NIFTY index quote: no")
            print(f"  needed a TOTP login for market data: {'yes' if logged_in else 'no'}")

            # The NIFTY contracts, from Kotak's own contract list.
            url = client.scrip_master(exchange_segment="nse_fo")
            fo = []
            if isinstance(url, str) and url.startswith("http"):
                async with httpx.AsyncClient(timeout=120, follow_redirects=True) as http:
                    fo = kotak_rows((await http.get(url)).text, "nse_fo")
            nifty = [r for r in fo if r.underlying == "NIFTY" and r.expiry and r.expiry >= today]
            futures = sorted((r for r in nifty if r.kind == KIND_FUTURE), key=lambda r: r.expiry)
            options = [r for r in nifty if r.kind == KIND_OPTION]
            print(f"  Kotak contract list: {len(nifty)} NIFTY contracts live ({len(futures)} futures, {len(options)} options)")

        # 2. How far back history goes.
        with section("2. How far back Kotak's NIFTY history goes (candles per window)"):
            symbol = None
            for candidate in ([f"nse_cm|{index}"] if index else []) + ["nse_cm|Nifty 50", "nse_cm|26000"]:
                got, err = history(client, candidate, "D", today - timedelta(days=20), today)
                if got:
                    symbol = candidate
                    break
            if symbol is None and futures:
                symbol = f"nse_fo|{futures[0].contract.token}"
                print("  the index has no history under any name tried: using the near-month future")
            print(f"  history for the NIFTY index: {'yes' if symbol and symbol.startswith('nse_cm') else 'no'}")
            if symbol:
                for interval, span, offsets in (("15min", 59, (0, 60, 120, 180, 365, 730, 1095, 1825)), ("5min", 29, (0, 30, 90, 365)),
                                                ("D", 179, (0, 180, 365, 730, 1825, 3650))):
                    cells = []
                    for back in offsets:
                        end = today - timedelta(days=back)
                        got, err = history(client, symbol, interval, end - timedelta(days=span), end)
                        first = min(c["ts"] for c in got).astimezone(IST).date() if got else None
                        cells.append(f"{back}d ago: {len(got)}" + (f" (from {first:%d %b %Y})" if first else f" ({err})" if err else ""))
                    print(f"  {interval:5} {span + 1}-day windows ending " + " | ".join(cells))
                _, err = history(client, symbol, "15min", today - timedelta(days=90), today)
                print(f"  a 90-day 15-min request: {'rejected (' + err + ')' if err else 'accepted'}")
            if kite:
                for interval, back, span in (("15m", 1825, 59), ("1d", 3650, 179)):
                    end = datetime.now(timezone.utc) - timedelta(days=back)
                    try:
                        got = await kite.get_historical_data("NIFTY 50", interval, end - timedelta(days=span), end, segment="NSE")
                        print(f"  Zerodha for contrast, {interval} {back}d ago: {len(got)} candles")
                    except Exception as exc:
                        print(f"  Zerodha for contrast, {interval} {back}d ago: failed ({type(exc).__name__})")

        # 3. Today's NIFTY 15-min candles against ours (synced from Zerodha).
        with section("3. The last session's NIFTY 15-min candles: Kotak against ours (from Zerodha)"):
            start_utc = datetime.combine(session, datetime.min.time(), IST).astimezone(timezone.utc)
            end_utc = start_utc + timedelta(days=1)
            ours = {as_key(c.ts): {"open": c.open, "high": c.high, "low": c.low, "close": c.close} for c in (await db.execute(
                select(OhlcvCandle).where(OhlcvCandle.instrument_id == nifty_id, OhlcvCandle.timeframe == "15m", OhlcvCandle.ts >= start_utc,
                                          OhlcvCandle.ts < end_utc)
            )).scalars()} if nifty_id else {}
            if symbol and symbol.startswith("nse_cm"):
                got, err = history(client, symbol, "15min", session, session)
                days = ", ".join(sorted({f"{as_ist(c['ts']):%d %b}" for c in got})) or "none"
                print(f"  Kotak's candles are of: {days}{' (' + err + ')' if err else ''}")
                theirs = {as_key(c["ts"]): c for c in got}
                compare("as stamped", ours, theirs)
                both = sorted(set(ours) & set(theirs))
                worst = sorted(both, key=lambda t: -abs(ours[t]["open"] - theirs[t]["open"]))[:3]
                for t in worst:
                    o, k = ours[t], theirs[t]
                    print(f"    {as_ist(datetime.fromtimestamp(t, tz=timezone.utc)):%H:%M}  ours O {o['open']:,.2f} H {o['high']:,.2f} L {o['low']:,.2f} "
                          f"C {o['close']:,.2f} | Kotak O {k['open']:,.2f} H {k['high']:,.2f} L {k['low']:,.2f} C {k['close']:,.2f}")
                # The 5 sessions before it too: how often closes agree.
                week = {as_key(c.ts): {"open": c.open, "high": c.high, "low": c.low, "close": c.close} for c in (await db.execute(
                    select(OhlcvCandle).where(OhlcvCandle.instrument_id == nifty_id, OhlcvCandle.timeframe == "15m",
                                              OhlcvCandle.ts >= start_utc - timedelta(days=8), OhlcvCandle.ts < end_utc))).scalars()}
                got, err = history(client, symbol, "15min", session - timedelta(days=8), session)
                compare("the last ~6 sessions", week, {as_key(c["ts"]): c for c in got})
                closes = [abs(week[t]["close"] - c["close"]) for t, c in ((as_key(c["ts"]), c) for c in got) if t in week]
                if closes:
                    print(f"    close gap: median {statistics.median(closes):.2f} pts, within 1 pt {sum(g <= 1 for g in closes)}/{len(closes)}, "
                          f"within 3 pts {sum(g <= 3 for g in closes)}/{len(closes)}")
            if kite and symbol and symbol.startswith("nse_cm"):
                try:
                    kd = await kite.get_historical_data("NIFTY 50", "1d", datetime.now(timezone.utc) - timedelta(days=45), datetime.now(timezone.utc), segment="NSE")
                    got, _ = history(client, symbol, "D", today - timedelta(days=45), today)
                    compare("daily, last 45 days vs Zerodha", {c["ts"].astimezone(IST).date(): c for c in kd},
                            {c["ts"].astimezone(IST).date(): c for c in got})
                except Exception as exc:
                    print(f"  daily vs Zerodha: failed ({type(exc).__name__})")

        # 4. The NIFTY future: OHLC, volume and OI against Zerodha.
        with section("4. NIFTY near-month future, the last session: Kotak against Zerodha"):
            if futures and kite:
                fut = futures[0]
                kite_symbol = (await db.execute(select(Instrument.symbol).where(
                    Instrument.exchange == "NFO", Instrument.instrument_type == "future", Instrument.expiry == fut.expiry,
                    Instrument.symbol.like("NIFTY%FUT")))).scalars().first()
                got, err = [], None
                for way, neosymbol, start in (("token, one day", f"nse_fo|{fut.contract.token}", session),
                                              ("token, 5 days", f"nse_fo|{fut.contract.token}", session - timedelta(days=5)),
                                              ("trading symbol", f"nse_fo|{fut.contract.symbol}", session - timedelta(days=5)),
                                              ("daily, token", f"nse_fo|{fut.contract.token}", session - timedelta(days=30))):
                    rows, err = history(client, neosymbol, "D" if way.startswith("daily") else "15min", start, session)
                    print(f"  future by {way}: {len(rows)} candles{' (' + err + ')' if err else ''}")
                    if rows and not way.startswith("daily"):
                        got = [c for c in rows if as_ist(c["ts"]).date() == session]
                        break
                print(f"  expiry {fut.expiry:%d %b}: Kotak {len(got)} candles of {session:%d %b}; Zerodha symbol found: {'yes' if kite_symbol else 'no'}")
                if kite_symbol:
                    try:
                        kd = await kite.get_historical_data(kite_symbol, "15m", start_utc, end_utc, segment="NFO")
                        compare("future 15-min", {as_key(c["ts"]): c for c in kd}, {as_key(c["ts"]): c for c in got},
                                ("open", "high", "low", "close", "volume"))
                        print(f"  OI in the candles: Kotak {sum(1 for c in got if num(c['oi'])) }/{len(got)}, "
                              f"Zerodha {sum(1 for c in kd if c.get('open_interest'))}/{len(kd)}")
                    except Exception as exc:
                        print(f"  Zerodha future: failed ({type(exc).__name__})")

        # 5. The option chain's OI against our latest PCR record.
        with section("5. Option chain: Kotak's OI against our latest PCR record"):
            resp = client.expiries(exchange="nse_fo", underlying="NIFTY", instrument_type="option")
            print(f"  expiries answer layout: {shape(resp)}")
            raw = resp.get("data", resp) if isinstance(resp, dict) else resp
            if isinstance(raw, dict):
                raw = next((v for v in raw.values() if isinstance(v, list)), [])
            expiries = sorted({e for e in (when_date(x) for x in (raw if isinstance(raw, list) else [])) if e and e >= today})
            print(f"  expiries listed: {len(expiries)}{' (' + failed(resp) + ')' if failed(resp) else ''}; nearest {expiries[0]:%d %b}" if expiries
                  else f"  expiries listed: 0 ({failed(resp) or 'unrecognised answer'})")
            snap = (await db.execute(select(PcrSnapshot).where(PcrSnapshot.underlying == "NIFTY", PcrSnapshot.source == SOURCE_LIVE)
                                     .order_by(PcrSnapshot.ts.desc()).limit(1))).scalars().first()
            expiry = expiries[0] if expiries else (min(r.expiry for r in options) if options else None)
            chain = None
            for count, on in ((120, expiry), (100, expiry), (80, expiry), (40, expiry), (40, None)):
                started = time.monotonic()
                resp = client.option_chain(exchange="nse_fo", underlying="NIFTY", expiry=on.isoformat() if on else None,
                                           instrument_type="option", count=count)
                data = resp.get("data") if isinstance(resp, dict) and isinstance(resp.get("data"), dict) else resp
                if isinstance(data, dict) and (data.get("call") or data.get("put")):
                    chain = data
                    print(f"  chain with count={count}: {len(data.get('call') or [])} calls, {len(data.get('put') or [])} puts "
                          f"({1000 * (time.monotonic() - started):.0f} ms)")
                    break
                print(f"  chain with count={count}{'' if on else ', no expiry given'}: no ({failed(resp) or 'empty'}); layout {shape(resp)}")
                time.sleep(PAUSE)
            kotak_oi = {}
            for side, kind in (("call", "CE"), ("put", "PE")):
                for row in (chain or {}).get(side) or []:
                    inst = pick(row, "inst", "instrument") or {}
                    oi = pick(row, "oi", "openInterest") or {}
                    strike = num(pick(inst, "strikePrice", "strike", "stkPrc", "sp"))
                    current = num(pick(oi, "cur", "current", "oi"))
                    if strike is not None and current is not None:
                        kotak_oi[(strike, pick(inst, "optionType", "optTp") or kind)] = (current, pick(inst, "neoSymbol", "neosymbol"))
            if chain and not kotak_oi:
                print(f"  a chain row's layout: {shape((chain.get('call') or [{}])[0], deepest=3)}")
            if snap and expiry:
                rows = (await db.execute(select(PcrStrikeOi).where(PcrStrikeOi.snapshot_id == snap.id, PcrStrikeOi.expiry == expiry))).scalars().all()
                ours_oi = {(r.strike, r.option_type): r.oi for r in rows if r.oi is not None}
                both = sorted(set(ours_oi) & set(kotak_oi))
                print(f"  our record: {snap.ts.astimezone(IST):%d %b %H:%M}, {len(ours_oi)} contracts of that expiry; in both: {len(both)}")
                if both:
                    gaps = [abs(kotak_oi[k][0] - ours_oi[k]) / ours_oi[k] * 100 for k in both if ours_oi[k]]
                    same = sum(kotak_oi[k][0] == ours_oi[k] for k in both)
                    print(f"  OI identical {same}/{len(both)}; difference median {statistics.median(gaps):.2f}%, largest {max(gaps):.2f}%")
                    for label, src in (("Kotak", {k: kotak_oi[k][0] for k in both}), ("ours ", {k: ours_oi[k] for k in both})):
                        calls = sum(v for (s, t), v in src.items() if t == "CE")
                        puts = sum(v for (s, t), v in src.items() if t == "PE")
                        print(f"  {label} over the same strikes: calls {calls / LAKH:,.1f} L, puts {puts / LAKH:,.1f} L, PCR {puts / calls:.3f}" if calls else "")

        # 6. A 50-contract quote.
        with section("6. Quotes for 50 option contracts near NIFTY: OI against our latest PCR record"):
            spot = snap.spot if snap and snap.spot else 0
            near = sorted((r for r in options if r.expiry == expiry and r.strike is not None), key=lambda r: abs(r.strike - spot))[:50]
            if near and snap:
                ours_oi = {(r.strike, r.option_type): r.oi for r in (await db.execute(
                    select(PcrStrikeOi).where(PcrStrikeOi.snapshot_id == snap.id, PcrStrikeOi.expiry == expiry))).scalars() if r.oi is not None}
                started = time.monotonic()
                resp = client.quotes(instrument_tokens=[{"instrument_token": r.contract.token, "exchange_segment": "nse_fo"} for r in near],
                                     quote_type="all")
                took = 1000 * (time.monotonic() - started)
                rows = quote_rows(resp)
                print(f"  50 at once: {len(rows)} quotes back in {took:.0f} ms" + (f" (refused, {fault_text(resp)})" if failed(resp) else ""))
                if not rows:
                    for size, quote_type in ((10, "all"), (1, "all"), (1, "oi"), (1, "ltp")):
                        resp = client.quotes(instrument_tokens=[{"instrument_token": r.contract.token, "exchange_segment": "nse_fo"}
                                                                for r in near[:size]], quote_type=quote_type)
                        got_rows = quote_rows(resp)
                        print(f"  {size} at once, {quote_type}: {len(got_rows)} back" + (f" (refused, {fault_text(resp)})" if failed(resp) else ""))
                        if got_rows:
                            rows = got_rows
                            print(f"  layout of one: {shape(rows[0])}")
                            break
                by_token = {str(r.get("exchange_token")): num(r.get("open_int")) for r in rows}
                pairs = [(by_token.get(r.contract.token), ours_oi.get((r.strike, r.option_type))) for r in near]
                pairs = [(k, o) for k, o in pairs if k is not None and o]
                if pairs:
                    gaps = [abs(k - o) / o * 100 for k, o in pairs]
                    print(f"  OI on both: {len(pairs)}; identical {sum(k == o for k, o in pairs)}; difference median {statistics.median(gaps):.2f}%, "
                          f"largest {max(gaps):.2f}%")
                if kotak_oi:
                    agree = sum(1 for r in near if (r.strike, r.option_type) in kotak_oi and by_token.get(r.contract.token) == kotak_oi[(r.strike, r.option_type)][0])
                    print(f"  quote OI same as the chain's: {agree}/{len(near)}")

        # 7. OI in an option's candles (what the PCR gap fill rebuilds from).
        with section("7. OI in Kotak's option candles"):
            atm = near[0] if near else None
            if atm:
                for way, neosymbol in (("token", f"nse_fo|{atm.contract.token}"), ("trading symbol", f"nse_fo|{atm.contract.symbol}")):
                    got, err = history(client, neosymbol, "15min", session - timedelta(days=5), session)
                    print(f"  the option nearest NIFTY by {way}, last 5 days: {len(got)} candles{' (' + err + ')' if err else ''}, "
                          f"with OI: {sum(1 for c in got if num(c['oi']))}")
                    if got:
                        break
        print(f"\nKotak answered 'too many requests' {RATE_LIMITED['times']} times (each retried after a wait)")
        if kite:
            await kite.aclose()


def as_key(ts: datetime) -> int:
    return int(ts.timestamp())


def as_ist(ts: datetime) -> datetime:
    return (ts if ts.tzinfo else ts.replace(tzinfo=timezone.utc)).astimezone(IST)


def when_date(value) -> date | None:
    if isinstance(value, dict):
        value = value.get("expiry") or value.get("expiryDate") or value.get("date")
    for fmt in ("%Y-%m-%d", "%d-%b-%Y", "%d%b%Y", "%d-%m-%Y", "%d %b %Y", "%Y%m%d", "%d-%b-%y"):
        try:
            return datetime.strptime(str(value), fmt).date()
        except ValueError:
            pass
    return None


if __name__ == "__main__":
    asyncio.run(main())
