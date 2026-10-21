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
import statistics
import time
from datetime import date, datetime, timedelta, timezone

import httpx
from sqlalchemy import select
from sqlalchemy.orm import selectinload

from app.core.encryption import decrypt_payload
from app.db.session import AsyncSessionLocal
from app.models.broker import Broker, BrokerAccount
from app.models.instrument import Instrument
from app.models.market_data import OhlcvCandle
from app.models.pcr import SOURCE_LIVE, PcrSnapshot, PcrStrikeOi
from app.services.broker.zerodha_broker import IST
from app.services.live_trading.broker_contracts import KIND_FUTURE, KIND_OPTION, kotak_rows
from app.services.options.pcr_snapshot_scheduler import kite_broker

PAUSE = 0.4  # between Kotak history calls: their rate limit isn't published
LAKH = 100_000.0


class section:
    """One numbered part of the check: a failure is reported and the rest still runs."""

    def __init__(self, title: str) -> None:
        print(f"\n{title}")

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
    resp = client.historical_data(neosymbol=neosymbol, interval=interval, from_date=start.isoformat(), to_date=end.isoformat())
    time.sleep(PAUSE)
    return candles_of(resp), failed(resp)


def quote_rows(resp) -> list[dict]:
    rows = resp.get("data", resp) if isinstance(resp, dict) else resp
    return rows if isinstance(rows, list) else []


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
        with section("3. Today's NIFTY 15-min candles: Kotak against ours (from Zerodha)"):
            nifty_id = (await db.execute(select(Instrument.id).where(Instrument.symbol == "NIFTY 50"))).scalars().first()
            start_utc = datetime.combine(today, datetime.min.time(), IST).astimezone(timezone.utc)
            ours = {as_key(c.ts): {"open": c.open, "high": c.high, "low": c.low, "close": c.close} for c in (await db.execute(
                select(OhlcvCandle).where(OhlcvCandle.instrument_id == nifty_id, OhlcvCandle.timeframe == "15m", OhlcvCandle.ts >= start_utc)
            )).scalars()} if nifty_id else {}
            if symbol and symbol.startswith("nse_cm"):
                got, err = history(client, symbol, "15min", today, today)
                theirs = {as_key(c["ts"]): c for c in got}
                compare("as stamped", ours, theirs)
                if theirs and len(set(ours) & set(theirs)) < len(theirs) // 2:
                    compare("Kotak stamped 15 min later (end of candle)", ours, {k - 900: v for k, v in theirs.items()})
                if got:
                    print(f"  Kotak's first/last today: {min(c['ts'] for c in got).astimezone(IST):%H:%M} / {max(c['ts'] for c in got).astimezone(IST):%H:%M}")
            if kite and symbol and symbol.startswith("nse_cm"):
                try:
                    kd = await kite.get_historical_data("NIFTY 50", "1d", datetime.now(timezone.utc) - timedelta(days=45), datetime.now(timezone.utc), segment="NSE")
                    got, _ = history(client, symbol, "D", today - timedelta(days=45), today)
                    compare("daily, last 45 days vs Zerodha", {c["ts"].astimezone(IST).date(): c for c in kd},
                            {c["ts"].astimezone(IST).date(): c for c in got})
                except Exception as exc:
                    print(f"  daily vs Zerodha: failed ({type(exc).__name__})")

        # 4. The NIFTY future: OHLC, volume and OI against Zerodha.
        with section("4. NIFTY near-month future today: Kotak against Zerodha"):
            if futures and kite:
                fut = futures[0]
                kite_symbol = (await db.execute(select(Instrument.symbol).where(
                    Instrument.exchange == "NFO", Instrument.instrument_type == "future", Instrument.expiry == fut.expiry,
                    Instrument.symbol.like("NIFTY%FUT")))).scalars().first()
                got, err = history(client, f"nse_fo|{fut.contract.token}", "15min", today, today)
                print(f"  expiry {fut.expiry:%d %b}: Kotak {len(got)} candles{' (' + err + ')' if err else ''}; Zerodha symbol found: {'yes' if kite_symbol else 'no'}")
                if kite_symbol:
                    try:
                        kd = await kite.get_historical_data(kite_symbol, "15m", start_utc, datetime.now(timezone.utc), segment="NFO")
                        compare("future 15-min", {as_key(c["ts"]): c for c in kd}, {as_key(c["ts"]): c for c in got},
                                ("open", "high", "low", "close", "volume"))
                        print(f"  OI in the candles: Kotak {sum(1 for c in got if num(c['oi'])) }/{len(got)}, "
                              f"Zerodha {sum(1 for c in kd if c.get('open_interest'))}/{len(kd)}")
                    except Exception as exc:
                        print(f"  Zerodha future: failed ({type(exc).__name__})")

        # 5. The option chain's OI against our latest PCR record.
        with section("5. Option chain: Kotak's OI against our latest PCR record"):
            resp = client.expiries(exchange="nse_fo", underlying="NIFTY", instrument_type="option")
            raw = resp.get("data", resp) if isinstance(resp, dict) else resp
            expiries = sorted({e for e in (when_date(x) for x in (raw if isinstance(raw, list) else [])) if e and e >= today})
            print(f"  expiries listed: {len(expiries)}{' (' + failed(resp) + ')' if failed(resp) else ''}; nearest {expiries[0]:%d %b}" if expiries
                  else f"  expiries listed: 0 ({failed(resp) or 'unrecognised answer'})")
            snap = (await db.execute(select(PcrSnapshot).where(PcrSnapshot.underlying == "NIFTY", PcrSnapshot.source == SOURCE_LIVE)
                                     .order_by(PcrSnapshot.ts.desc()).limit(1))).scalars().first()
            expiry = expiries[0] if expiries else (min(r.expiry for r in options) if options else None)
            chain = None
            for count in (120, 100, 80, 40):
                started = time.monotonic()
                resp = client.option_chain(exchange="nse_fo", underlying="NIFTY", expiry=expiry.isoformat() if expiry else None,
                                           instrument_type="option", count=count)
                data = resp.get("data") if isinstance(resp, dict) else None
                if isinstance(data, dict) and (data.get("call") or data.get("put")):
                    chain = data
                    print(f"  chain with count={count}: {len(data.get('call') or [])} calls, {len(data.get('put') or [])} puts "
                          f"({1000 * (time.monotonic() - started):.0f} ms)")
                    break
                print(f"  chain with count={count}: no ({failed(resp) or 'empty'})")
                time.sleep(PAUSE)
            kotak_oi = {}
            for side in ("call", "put"):
                for row in (chain or {}).get(side) or []:
                    inst, oi = row.get("instrument") or {}, row.get("openInterest") or {}
                    strike, kind = num(inst.get("strikePrice")), inst.get("optionType")
                    if strike is not None and kind and num(oi.get("current")) is not None:
                        kotak_oi[(strike, kind)] = (num(oi["current"]), inst.get("neoSymbol"))
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
        with section("6. Quote for 50 option contracts"):
            tokens = [v[1] for v in kotak_oi.values() if v[1]][:50]
            if tokens:
                started = time.monotonic()
                resp = client.quotes(instrument_tokens=[{"instrument_token": t.split("|")[1], "exchange_segment": t.split("|")[0]} for t in tokens],
                                     quote_type="all")
                took = 1000 * (time.monotonic() - started)
                rows = quote_rows(resp)
                with_oi = [r for r in rows if num(r.get("open_int"))]
                by_token = {f"{r.get('exchange')}|{r.get('exchange_token')}": num(r.get("open_int")) for r in rows}
                agree = sum(1 for v in kotak_oi.values() if v[1] in by_token and by_token[v[1]] == v[0])
                print(f"  {len(rows)} quotes back in {took:.0f} ms{' (' + failed(resp) + ')' if failed(resp) else ''}; with OI: {len(with_oi)}; "
                      f"OI same as the chain: {agree}/{len(tokens)}")

        # 7. OI in an option's candles (what the PCR gap fill rebuilds from).
        with section("7. OI in Kotak's option candles"):
            if kotak_oi:
                spot = num(index and quote_rows(client.quotes(instrument_tokens=[{"instrument_token": index, "exchange_segment": "nse_cm"}],
                                                              quote_type="ltp"))[0].get("ltp")) or 0
                (strike, kind), (oi, token) = min(kotak_oi.items(), key=lambda kv: abs(kv[0][0] - spot))
                got, err = history(client, token, "15min", today, today)
                print(f"  ATM {kind} today: {len(got)} candles{' (' + err + ')' if err else ''}, with OI: {sum(1 for c in got if num(c['oi']))}")
        if kite:
            await kite.aclose()


def as_key(ts: datetime) -> int:
    return int(ts.timestamp())


def when_date(value) -> date | None:
    if isinstance(value, dict):
        value = value.get("expiry") or value.get("expiryDate") or value.get("date")
    for fmt in ("%Y-%m-%d", "%d-%b-%Y", "%d%b%Y", "%d-%m-%Y"):
        try:
            return datetime.strptime(str(value), fmt).date()
        except ValueError:
            pass
    return None


if __name__ == "__main__":
    asyncio.run(main())
