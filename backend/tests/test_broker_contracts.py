"""Matching this app's instruments to each broker's own contract
(services/live_trading/broker_contracts.py), and the protected limit price
every live order is sent at (native_gateway.protected_limit)."""

from datetime import date

import pytest

from app.models.instrument import Instrument
from app.services.live_trading import broker_contracts
from app.services.live_trading.broker_contracts import (
    ContractIndex,
    ContractListError,
    ContractNotFound,
    angel_rows,
    dhan_rows,
    kite_rows,
)
from app.services.live_trading.native_gateway import protected_limit

EXPIRY = date(2026, 10, 6)


def _option(symbol="NIFTY2610622700CE", strike=22700.0, option_type="CE", lot=65, expiry=EXPIRY):
    return Instrument(exchange="NFO", symbol=symbol, name="x", instrument_type="option", data_source="t", external_ref=symbol,
                      strike=strike, option_type=option_type, lot_size=lot, expiry=expiry)


def _future(symbol="NIFTY26OCTFUT", lot=65, expiry=date(2026, 10, 27)):
    return Instrument(exchange="NFO", symbol=symbol, name="x", instrument_type="future", data_source="t", external_ref=symbol,
                      lot_size=lot, expiry=expiry)


def _stock(symbol="SBIN"):
    return Instrument(exchange="NSE", symbol=symbol, name="x", instrument_type="equity", data_source="t", external_ref=symbol)


DHAN_CSV = (
    "SEM_EXM_EXCH_ID,SEM_SEGMENT,SEM_SMST_SECURITY_ID,SEM_INSTRUMENT_NAME,SEM_EXPIRY_CODE,SEM_TRADING_SYMBOL,SEM_LOT_UNITS,"
    "SEM_CUSTOM_SYMBOL,SEM_EXPIRY_DATE,SEM_STRIKE_PRICE,SEM_OPTION_TYPE,SEM_TICK_SIZE,SEM_EXPIRY_FLAG,SEM_EXCH_INSTRUMENT_TYPE,"
    "SEM_SERIES,SM_SYMBOL_NAME\n"
    "NSE,E,3045,EQUITY,,SBIN,1.0,SBI,,,XX,5.0,,ES,EQ,STATE BANK OF INDIA\n"
    "BSE,E,500112,EQUITY,,SBIN,1.0,SBI,,,XX,5.0,,ES,A,STATE BANK OF INDIA\n"
    "NSE,D,40001,OPTIDX,0,NIFTY-Oct2026-22700-CE,65.0,NIFTY 06 OCT 22700 CALL,2026-10-06 14:30:00,22700.00000,CE,5.0,W,OP,NA,\n"
    "NSE,D,40002,OPTIDX,0,NIFTY-Oct2026-22700-PE,65.0,NIFTY 06 OCT 22700 PUT,2026-10-06 14:30:00,22700.00000,PE,5.0,W,OP,NA,\n"
    "NSE,D,40003,OPTIDX,0,NIFTY-Oct2026-22700-CE,65.0,NIFTY 13 OCT 22700 CALL,2026-10-13 14:30:00,22700.00000,CE,5.0,W,OP,NA,\n"
    "NSE,D,40010,FUTIDX,0,NIFTY-Oct2026-FUT,65.0,NIFTY OCT FUT,2026-10-27 14:30:00,-0.01000,XX,10.0,M,FUTIDX,NA,\n"
    "NSE,D,50001,FUTSTK,0,BAJAJ-AUTO-Oct2026-FUT,75.0,BAJAJ-AUTO OCT FUT,2026-10-27 14:30:00,-0.01000,XX,10.0,M,FUTSTK,NA,\n"
)

ANGEL = [
    {"token": "3045", "symbol": "SBIN-EQ", "name": "SBIN", "expiry": "", "strike": "-1.000000", "lotsize": "1", "instrumenttype": "",
     "exch_seg": "NSE", "tick_size": "5.000000"},
    {"token": "99926000", "symbol": "Nifty 50", "name": "NIFTY", "expiry": "", "strike": "0.000000", "lotsize": "1",
     "instrumenttype": "AMXIDX", "exch_seg": "NSE", "tick_size": "0.000000"},
    {"token": "40001", "symbol": "NIFTY06OCT2622700CE", "name": "NIFTY", "expiry": "06OCT2026", "strike": "2270000.000000",
     "lotsize": "65", "instrumenttype": "OPTIDX", "exch_seg": "NFO", "tick_size": "5.000000"},
    {"token": "40010", "symbol": "NIFTY27OCT26FUT", "name": "NIFTY", "expiry": "27OCT2026", "strike": "-1.000000", "lotsize": "65",
     "instrumenttype": "FUTIDX", "exch_seg": "NFO", "tick_size": "10.000000"},
    {"token": "60001", "symbol": "NIFTYNXT5006OCT2622700CE", "name": "NIFTYNXT50", "expiry": "06OCT2026", "strike": "2270000.000000",
     "lotsize": "25", "instrumenttype": "OPTIDX", "exch_seg": "NFO", "tick_size": "5.000000"},
]


def test_dhan_contracts_are_matched_by_what_they_are():
    index = ContractIndex(dhan_rows(DHAN_CSV))
    stock = index.match(_stock())
    assert (stock.key, stock.token, stock.exchange) == (("NSE_EQ", "3045"), "3045", "NSE_EQ")  # NSE, not BSE
    option = index.match(_option())
    assert (option.token, option.exchange, option.lot_size) == ("40001", "NSE_FNO", 65)  # 6 Oct, not 13 Oct
    assert index.match(_option(symbol="NIFTY2610622700PE", option_type="PE")).token == "40002"
    assert index.match(_future()).token == "40010"
    assert index.match(_future("BAJAJ-AUTO26OCTFUT", lot=75)).token == "50001"  # a hyphen in the underlying


def test_angel_one_contracts_strikes_in_paise_and_longest_underlying_wins():
    index = ContractIndex(angel_rows(ANGEL))
    stock = index.match(_stock())
    assert (stock.key, stock.symbol) == (("NSE", "3045"), "SBIN-EQ")
    option = index.match(_option())
    assert (option.symbol, option.token, option.exchange) == ("NIFTY06OCT2622700CE", "40001", "NFO")
    nxt = index.match(_option(symbol="NIFTYNXT502610622700CE", lot=25))
    assert nxt.token == "60001"  # NIFTY is a prefix too, but the year doesn't follow it
    assert index.match(_future()).symbol == "NIFTY27OCT26FUT"


def test_kite_rows_and_the_be_series():
    rows = kite_rows([
        {"tradingsymbol": "SBIN", "name": "STATE BANK", "expiry": "", "strike": "0", "lot_size": "1", "instrument_type": "EQ",
         "exchange": "NSE", "exchange_token": "3045"},
        {"tradingsymbol": "XYZ-BE", "name": "XYZ", "expiry": "", "strike": "0", "lot_size": "1", "instrument_type": "EQ",
         "exchange": "NSE", "exchange_token": "9"},
        {"tradingsymbol": "NIFTY2610622700CE", "name": "NIFTY", "expiry": "2026-10-06", "strike": "22700.0", "lot_size": "65",
         "instrument_type": "CE", "exchange": "NFO", "exchange_token": "40001"},
    ])
    index = ContractIndex(rows)
    assert index.match(_stock()).key == ("NSE", "SBIN")
    assert index.match(_stock("XYZ")).symbol == "XYZ-BE"
    assert index.match(_option()).key == ("NFO", "NIFTY2610622700CE")


def test_anything_but_one_exact_match_is_refused():
    index = ContractIndex(angel_rows(ANGEL))
    with pytest.raises(ContractNotFound, match="no OPT expiring 06 Oct 2026 at 22800 CE"):
        index.match(_option(symbol="NIFTY2610622800CE", strike=22800.0))
    with pytest.raises(ContractNotFound, match="lot size 65 at the broker, 75 here"):
        index.match(_option(lot=75))
    with pytest.raises(ContractNotFound, match="not in the broker's NSE stock list"):
        index.match(_stock("NOSUCH"))
    with pytest.raises(ContractNotFound, match="expiry, strike or CE/PE unknown"):
        index.match(_option(expiry=None))
    doubled = ContractIndex(angel_rows(ANGEL + [dict(ANGEL[2], token="40099")]))
    with pytest.raises(ContractNotFound, match="2 contracts at the broker match"):
        doubled.match(_option())


def test_a_changed_list_format_is_an_error_not_a_guess():
    with pytest.raises(ContractListError, match="Dhan's contract list has changed format -- missing SEM_SMST_SECURITY_ID"):
        dhan_rows(DHAN_CSV.replace("SEM_SMST_SECURITY_ID", "SECURITY"))
    with pytest.raises(ContractListError, match="missing lotsize"):
        angel_rows([{k: v for k, v in ANGEL[0].items() if k != "lotsize"}])


async def test_lists_are_fetched_once_a_day_and_a_failed_fetch_falls_back_a_little(monkeypatch):
    broker_contracts.forget_lists()
    fetched = []

    async def fake_fetch(broker_code, broker):
        fetched.append(broker_code)
        if len(fetched) > 2:
            raise ContractListError("down")
        return angel_rows(ANGEL)

    monkeypatch.setattr(broker_contracts, "_fetch_rows", fake_fetch)
    try:
        day = date(2026, 10, 5)
        await broker_contracts.contract_for("angel_one", None, _stock(), day)
        await broker_contracts.contract_for("angel_one", None, _stock(), day)
        assert fetched == ["angel_one"]  # once that day
        await broker_contracts.contract_for("angel_one", None, _stock(), date(2026, 10, 6))
        await broker_contracts.contract_for("angel_one", None, _stock(), date(2026, 10, 9))  # fetch fails: the 6 Oct list
        with pytest.raises(ContractListError, match="down"):
            await broker_contracts.contract_for("angel_one", None, _stock(), date(2026, 10, 12))  # too old to use
    finally:
        broker_contracts.forget_lists()


@pytest.mark.parametrize("instrument, side, price, limit", [
    (_option(), "buy", 150.0, 151.5),  # 1% on the 0.05 grid
    (_option(), "sell", 150.0, 148.5),
    (_option(), "buy", 2.0, 2.05),  # 1% is under a step: one step
    (_option(), "sell", 0.05, 0.05),  # never zero
    (_future(), "buy", 25013.4, 25263.6),  # 25263.534 -> up to the 0.10 grid
    (_stock(), "buy", 800.0, 808.0),
    (_stock(), "sell", 7.5, 7.4),  # 7.425 -> down to the 0.10 grid
    (_stock(), "buy", 12000.0, 12120.0),
    (_stock(), "sell", 30000.0, 29700.0),
])
def test_the_protected_limit(instrument, side, price, limit):
    assert protected_limit(instrument, side, price) == pytest.approx(limit)


class _Holder:
    def __init__(self, positions, holdings):
        self.positions, self.holdings = positions, holdings

    async def get_positions(self):
        return self.positions

    async def get_holdings(self):
        return self.holdings


async def test_positions_and_holdings_are_keyed_as_each_broker_names_contracts():
    from app.services.live_trading.native_gateway import BrokerGateway

    dhan = BrokerGateway("dhan", _Holder(
        [{"securityId": "40001", "exchangeSegment": "NSE_FNO", "netQty": -65}, {"securityId": "3045", "exchangeSegment": "NSE_EQ", "netQty": 2}],
        [{"securityId": "3045", "exchange": "ALL", "totalQty": 10, "t1Qty": 3}],
    ))
    assert await dhan.net_positions() == {("NSE_FNO", "40001"): -65.0, ("NSE_EQ", "3045"): 12.0}
    angel = BrokerGateway("angel_one", _Holder(
        [{"symboltoken": "40001", "exchange": "NFO", "netqty": "-65"}],
        [{"symboltoken": "3045", "exchange": "NSE", "quantity": 7, "t1quantity": 3}],
    ))
    assert await angel.net_positions() == {("NFO", "40001"): -65.0, ("NSE", "3045"): 10.0}
    # the same keys the contract lists give them
    assert ContractIndex(dhan_rows(DHAN_CSV)).match(_option()).key == ("NSE_FNO", "40001")
    assert ContractIndex(angel_rows(ANGEL)).match(_stock()).key == ("NSE", "3045")
