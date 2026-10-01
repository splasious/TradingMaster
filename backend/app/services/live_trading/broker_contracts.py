"""Which contract at a broker is the one a live strategy means.

This app names every instrument the way Zerodha does (Instrument.symbol /
external_ref, e.g. "SBIN", "NIFTY2610622700CE"). Each other broker has its
own names and ids -- Dhan a numeric security id, Angel One a token plus its
own symbol ("NIFTY06OCT2622700CE"), Kotak Neo its own trading symbol -- so
an order can't simply carry ours. Each broker publishes a contract list;
it's fetched once a day (IST) and our instrument is matched against it on
what the contract *is*, not on its name:

  stock    NSE, the same symbol, series EQ (or BE)
  future   NFO, the same underlying and expiry date
  option   NFO, the same underlying, expiry date, strike and CE/PE

The underlying is the broker's own name for it ("NIFTY", "BAJAJ-AUTO"),
recognised as the start of our symbol followed by the expiry's two-digit
year -- Zerodha's names always start that way -- so no name format is
parsed on our side. An F&O match must also agree on the lot size: the
strategy sizes in lots, and a different lot would trade a different
quantity. Anything other than exactly one match is an error
(ContractNotFound) -- the order isn't sent.

A list that can't be fetched falls back to the last one fetched within
STALE_LIST_DAYS (contracts already listed don't change); with none, it's an
error (ContractListError). A list whose columns aren't the expected ones is
an error too -- never a guess.

Read from each broker's own published material, not invented:
  Zerodha   Kite's instrument dump (zerodha_broker.get_instruments): the
            CSV columns tradingsymbol, name, expiry, strike, lot_size,
            instrument_type, exchange, exchange_token
  Dhan      the scrip master its official SDK downloads (dhanhq 2.2.0,
            _security.py: COMPACT_CSV_URL)
  Angel One the OpenAPI scrip master its SmartAPI docs point to: token,
            symbol, name, expiry (DDMONYYYY), strike (x100), lotsize,
            instrumenttype, exch_seg
  Kotak Neo the per-segment CSVs its SDK's scrip_master() points to
            (kotakneoapi 3.0.7): pSymbol (token), pTrdSymbol, pSymbolName,
            pOptionType (CE/PE/XX), pExpiryDate (1980-based on F&O),
            "dStrikePrice;" (x100), lLotSize
The Dhan, Angel One and Kotak column sets are checked on every load; the broker
test (Settings > Brokers) shows what a sample of contracts matched to.
"""

import asyncio
import csv
import io
import json
import logging
import re
from collections import defaultdict
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from typing import Any

import httpx

from app.models.instrument import Instrument

logger = logging.getLogger(__name__)

DHAN_CONTRACTS_URL = "https://images.dhan.co/api-data/api-scrip-master.csv"
ANGEL_CONTRACTS_URL = "https://margincalculator.angelbroking.com/OpenAPI_File/files/OpenAPIScripMaster.json"
FETCH_TIMEOUT_SECONDS = 120.0
STALE_LIST_DAYS = 3

KIND_STOCK, KIND_FUTURE, KIND_OPTION = "EQ", "FUT", "OPT"
_STOCK_SERIES = ("EQ", "BE")


class ContractListError(Exception):
    """A broker's contract list couldn't be fetched or read."""


class ContractNotFound(Exception):
    """Not exactly one contract at the broker is ours."""


@dataclass(frozen=True)
class BrokerContract:
    key: tuple[str, str]  # how the broker reports it in positions / holdings
    symbol: str  # the broker's trading symbol
    exchange: str  # the broker's exchange / segment code for an order
    token: str | None = None  # the broker's numeric id, where it has one
    lot_size: int | None = None


@dataclass(frozen=True)
class ContractRow:
    kind: str
    underlying: str
    expiry: date | None
    strike: float | None
    option_type: str | None
    series: str | None
    contract: BrokerContract


def _strike_key(strike: float | None) -> float | None:
    return None if strike is None else round(float(strike), 2)


def _kind_of(instrument: Instrument) -> str:
    if instrument.instrument_type == "option":
        return KIND_OPTION
    if instrument.instrument_type == "future":
        return KIND_FUTURE
    return KIND_STOCK


class ContractIndex:
    def __init__(self, rows: list[ContractRow]) -> None:
        self.size = len(rows)
        self._stocks: dict[str, list[ContractRow]] = defaultdict(list)
        self._derivatives: dict[tuple, list[ContractRow]] = defaultdict(list)
        for row in rows:
            if row.kind == KIND_STOCK:
                self._stocks[row.underlying.upper()].append(row)
            else:
                self._derivatives[(row.kind, row.expiry, _strike_key(row.strike), row.option_type)].append(row)

    def match(self, instrument: Instrument) -> BrokerContract:
        kind = _kind_of(instrument)
        what = f"{instrument.symbol}"
        if kind == KIND_STOCK:
            found = self._stocks.get(instrument.symbol.upper(), [])
            for series in _STOCK_SERIES:
                in_series = [r for r in found if (r.series or "EQ") == series]
                if len(in_series) == 1:
                    return in_series[0].contract
                if len(in_series) > 1:
                    raise ContractNotFound(f"{what}: {len(in_series)} {series} contracts match")
            raise ContractNotFound(f"{what}: not in the broker's NSE stock list")

        if instrument.expiry is None or (kind == KIND_OPTION and (instrument.strike is None or not instrument.option_type)):
            raise ContractNotFound(f"{what}: expiry, strike or CE/PE unknown")
        strike = _strike_key(instrument.strike) if kind == KIND_OPTION else None
        option_type = instrument.option_type.upper() if kind == KIND_OPTION else None
        candidates = self._derivatives.get((kind, instrument.expiry, strike, option_type), [])
        year = f"{instrument.expiry.year % 100:02d}"
        ours = instrument.symbol.upper()
        named = [r for r in candidates if r.underlying and ours.startswith(r.underlying.upper())
                 and ours[len(r.underlying):].startswith(year)]
        if not named:
            raise ContractNotFound(f"{what}: no {kind} expiring {instrument.expiry:%d %b %Y}"
                                   + (f" at {instrument.strike:g} {option_type}" if kind == KIND_OPTION else "") + " in the broker's list")
        longest = max(len(r.underlying) for r in named)
        named = [r for r in named if len(r.underlying) == longest]
        if len(named) > 1:
            raise ContractNotFound(f"{what}: {len(named)} contracts at the broker match")
        contract = named[0].contract
        if instrument.lot_size and contract.lot_size and int(contract.lot_size) != int(instrument.lot_size):
            raise ContractNotFound(f"{what}: lot size {contract.lot_size} at the broker, {instrument.lot_size} here")
        if instrument.lot_size and not contract.lot_size:
            raise ContractNotFound(f"{what}: the broker's list gives no lot size")
        return contract


# ------------------------------------------------------------- parsing --

def _require_columns(have, need: tuple[str, ...], whose: str) -> None:
    missing = [c for c in need if c not in have]
    if missing:
        raise ContractListError(f"{whose} contract list has changed format -- missing {', '.join(missing)}")


def _float(value: Any) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _int(value: Any) -> int | None:
    number = _float(value)
    return int(round(number)) if number else None


_KITE_COLUMNS = ("tradingsymbol", "name", "expiry", "strike", "lot_size", "instrument_type", "exchange")


def kite_rows(dump: list[dict]) -> list[ContractRow]:
    """Zerodha's own instrument dump (NSE + NFO segments)."""
    if dump:
        _require_columns(dump[0].keys(), _KITE_COLUMNS, "Zerodha's")
    rows = []
    for raw in dump:
        exchange, kind_raw = raw.get("exchange"), (raw.get("instrument_type") or "").upper()
        symbol = raw.get("tradingsymbol") or ""
        underlying = symbol
        if exchange == "NSE" and kind_raw == "EQ":
            kind, series = KIND_STOCK, "EQ"
            if symbol.endswith("-BE"):  # a stock moved to the BE series trades as "<symbol>-BE"
                underlying, series = symbol[:-3], "BE"
        elif exchange == "NFO" and kind_raw in ("CE", "PE", "FUT"):
            kind, series = (KIND_FUTURE if kind_raw == "FUT" else KIND_OPTION), None
        else:
            continue
        expiry = None
        if kind != KIND_STOCK:
            try:
                expiry = date.fromisoformat(str(raw.get("expiry"))[:10])
            except ValueError:
                continue
        rows.append(ContractRow(
            kind=kind, underlying=(underlying if kind == KIND_STOCK else raw.get("name") or ""), expiry=expiry,
            strike=_float(raw.get("strike")) if kind == KIND_OPTION else None,
            option_type=kind_raw if kind == KIND_OPTION else None, series=series,
            contract=BrokerContract(key=(exchange, symbol), symbol=symbol, exchange=exchange,
                                    token=str(raw.get("exchange_token") or "") or None,
                                    lot_size=_int(raw.get("lot_size")) if kind != KIND_STOCK else None),
        ))
    return rows


_DHAN_COLUMNS = (
    "SEM_EXM_EXCH_ID", "SEM_SEGMENT", "SEM_SMST_SECURITY_ID", "SEM_INSTRUMENT_NAME", "SEM_TRADING_SYMBOL",
    "SEM_LOT_UNITS", "SEM_EXPIRY_DATE", "SEM_STRIKE_PRICE", "SEM_OPTION_TYPE", "SEM_SERIES",
)
_DHAN_FNO = {"OPTIDX": KIND_OPTION, "OPTSTK": KIND_OPTION, "FUTIDX": KIND_FUTURE, "FUTSTK": KIND_FUTURE}
# "NIFTY-Oct2026-22700-CE", "BAJAJ-AUTO-Oct2026-FUT": the underlying is
# everything before "-<Mon><YYYY>-".
_DHAN_UNDERLYING = re.compile(r"^(.+?)-[A-Za-z]{3}\d{4}-")


def dhan_rows(text: str) -> list[ContractRow]:
    reader = csv.DictReader(io.StringIO(text))
    _require_columns([c.strip() for c in reader.fieldnames or []], _DHAN_COLUMNS, "Dhan's")
    rows = []
    for raw in reader:
        raw = {k.strip(): (v or "").strip() for k, v in raw.items() if k}
        if raw["SEM_EXM_EXCH_ID"] != "NSE":
            continue
        security_id, symbol = raw["SEM_SMST_SECURITY_ID"], raw["SEM_TRADING_SYMBOL"]
        instrument_name = raw["SEM_INSTRUMENT_NAME"].upper()
        if raw["SEM_SEGMENT"] == "E" and instrument_name == "EQUITY":
            rows.append(ContractRow(KIND_STOCK, symbol, None, None, None, raw["SEM_SERIES"].upper() or None,
                                    BrokerContract(("NSE_EQ", security_id), symbol, "NSE_EQ", security_id)))
            continue
        kind = _DHAN_FNO.get(instrument_name)
        if raw["SEM_SEGMENT"] != "D" or kind is None:
            continue
        underlying = _DHAN_UNDERLYING.match(symbol)
        try:
            expiry = date.fromisoformat(raw["SEM_EXPIRY_DATE"][:10])
        except ValueError:
            continue
        option_type = raw["SEM_OPTION_TYPE"].upper() if kind == KIND_OPTION else None
        rows.append(ContractRow(
            kind, underlying.group(1) if underlying else "", expiry,
            _float(raw["SEM_STRIKE_PRICE"]) if kind == KIND_OPTION else None, option_type, None,
            BrokerContract(("NSE_FNO", security_id), symbol, "NSE_FNO", security_id, _int(raw["SEM_LOT_UNITS"])),
        ))
    return rows


_ANGEL_FIELDS = ("token", "symbol", "name", "expiry", "strike", "lotsize", "instrumenttype", "exch_seg")
_ANGEL_FNO = {"OPTIDX": KIND_OPTION, "OPTSTK": KIND_OPTION, "FUTIDX": KIND_FUTURE, "FUTSTK": KIND_FUTURE}


def angel_rows(data: list[dict]) -> list[ContractRow]:
    if not isinstance(data, list):
        raise ContractListError("Angel One's contract list isn't a list")
    if data:
        _require_columns(data[0].keys(), _ANGEL_FIELDS, "Angel One's")
    rows = []
    for raw in data:
        segment, symbol, token = raw.get("exch_seg"), str(raw.get("symbol") or ""), str(raw.get("token") or "")
        if segment == "NSE":
            if "-" not in symbol:
                continue  # indices and the like
            name, series = symbol.rsplit("-", 1)
            if series.upper() not in _STOCK_SERIES:
                continue
            rows.append(ContractRow(KIND_STOCK, name, None, None, None, series.upper(),
                                    BrokerContract(("NSE", token), symbol, "NSE", token)))
            continue
        kind = _ANGEL_FNO.get(str(raw.get("instrumenttype") or "").upper())
        if segment != "NFO" or kind is None:
            continue
        try:
            expiry = datetime.strptime(str(raw.get("expiry")).upper(), "%d%b%Y").date()
        except ValueError:
            continue
        strike = _float(raw.get("strike"))
        rows.append(ContractRow(
            kind, str(raw.get("name") or ""), expiry,
            (strike / 100.0 if strike is not None else None) if kind == KIND_OPTION else None,  # listed in paise
            symbol[-2:].upper() if kind == KIND_OPTION else None, None,
            BrokerContract(("NFO", token), symbol, "NFO", token, _int(raw.get("lotsize"))),
        ))
    return rows


_KOTAK_COLUMNS = ("pSymbol", "pTrdSymbol", "pSymbolName", "pOptionType", "pExpiryDate", "dStrikePrice", "lLotSize")
# Kotak's F&O expiries are seconds from 1 Jan 1980 (the SDK's search_scrip
# adds this to read them as Unix time).
_KOTAK_EPOCH_SHIFT = 315511200


def kotak_rows(text: str, segment: str) -> list[ContractRow]:
    """One of Kotak Neo's per-segment contract lists (nse_cm or nse_fo)."""
    reader = csv.DictReader(io.StringIO(text))
    # "dStrikePrice;" carries a stray semicolon in Kotak's header.
    fields = {(c or "").strip().rstrip(";"): c for c in reader.fieldnames or []}
    _require_columns(fields, _KOTAK_COLUMNS, "Kotak Neo's")
    rows = []
    for raw in reader:
        get = lambda name: (raw.get(fields[name]) or "").strip()  # noqa: E731
        token, symbol, name = get("pSymbol"), get("pTrdSymbol"), get("pSymbolName")
        if not token or not symbol:
            continue
        if segment == "nse_cm":
            if "-" not in symbol:
                continue
            series = symbol.rsplit("-", 1)[1].upper()
            if series not in _STOCK_SERIES:
                continue
            rows.append(ContractRow(KIND_STOCK, symbol.rsplit("-", 1)[0], None, None, None, series,
                                    BrokerContract((segment, token), symbol, segment, token)))
            continue
        option_type = get("pOptionType").upper()
        kind = KIND_OPTION if option_type in ("CE", "PE") else KIND_FUTURE if option_type == "XX" else None
        expiry_raw = _float(get("pExpiryDate"))
        if kind is None or not expiry_raw:
            continue
        expiry = datetime.fromtimestamp(int(expiry_raw) + _KOTAK_EPOCH_SHIFT, tz=timezone.utc).date()
        strike = _float(get("dStrikePrice"))
        rows.append(ContractRow(
            kind, name, expiry, (strike / 100.0 if strike is not None else None) if kind == KIND_OPTION else None,  # listed x100
            option_type if kind == KIND_OPTION else None, None,
            BrokerContract((segment, token), symbol, segment, token, _int(get("lLotSize"))),
        ))
    return rows


# --------------------------------------------------------------- fetching --

async def _download(url: str) -> bytes:
    try:
        async with httpx.AsyncClient(timeout=FETCH_TIMEOUT_SECONDS, follow_redirects=True) as client:
            response = await client.get(url)
    except httpx.HTTPError as exc:
        raise ContractListError(f"couldn't download the contract list: {exc}") from exc
    if response.status_code != 200:
        raise ContractListError(f"contract list download failed (HTTP {response.status_code})")
    return response.content


async def _fetch_rows(broker_code: str, broker: Any) -> list[ContractRow]:
    if broker_code == "zerodha_kite":
        return kite_rows(list(await broker.get_instruments("NSE")) + list(await broker.get_instruments("NFO")))
    if broker_code == "dhan":
        return dhan_rows((await _download(DHAN_CONTRACTS_URL)).decode("utf-8", errors="replace"))
    if broker_code == "angel_one":
        try:
            data = json.loads(await _download(ANGEL_CONTRACTS_URL))
        except ValueError as exc:
            raise ContractListError("Angel One's contract list isn't valid JSON") from exc
        return angel_rows(data)
    if broker_code == "kotak_neo":
        try:
            urls = await broker.contract_list_urls()
        except Exception as exc:
            raise ContractListError(f"Kotak Neo's contract list address: {exc}") from exc
        rows: list[ContractRow] = []
        for segment, url in urls.items():
            rows += kotak_rows((await _download(url)).decode("utf-8", errors="replace"), segment)
        return rows
    raise ContractListError(f"no contract list for broker '{broker_code}'")


_indexes: dict[str, tuple[date, ContractIndex]] = {}
_locks: dict[str, asyncio.Lock] = defaultdict(asyncio.Lock)


async def contract_index(broker_code: str, broker: Any, today: date) -> ContractIndex:
    cached = _indexes.get(broker_code)
    if cached is not None and cached[0] == today:
        return cached[1]
    async with _locks[broker_code]:
        cached = _indexes.get(broker_code)
        if cached is not None and cached[0] == today:
            return cached[1]
        try:
            index = ContractIndex(await _fetch_rows(broker_code, broker))
        except ContractListError as exc:
            if cached is not None and today - cached[0] <= timedelta(days=STALE_LIST_DAYS):
                logger.warning("Using %s's contract list from %s: %s", broker_code, cached[0], exc)
                return cached[1]
            raise
        if index.size == 0:
            raise ContractListError(f"{broker_code}'s contract list is empty")
        _indexes[broker_code] = (today, index)
        return index


def forget_lists() -> None:
    _indexes.clear()


async def contract_for(broker_code: str, broker: Any, instrument: Instrument, today: date) -> BrokerContract:
    return (await contract_index(broker_code, broker, today)).match(instrument)
