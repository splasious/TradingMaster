import pytest
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession


async def _login(client: AsyncClient, email: str, password: str) -> str:
    resp = await client.post("/api/v1/auth/login", json={"email": email, "password": password})
    assert resp.status_code == 200
    return resp.json()["access_token"]


async def _default_portfolio_id(client: AsyncClient, headers: dict) -> str:
    resp = await client.get("/api/v1/paper-trading/portfolios", headers=headers)
    return resp.json()[0]["id"]


TRIVIAL_NATIVE_CODE = (
    "async def evaluate(ctx):\n"
    "    ctx.state['calls'] = ctx.state.get('calls', 0) + 1\n"
    "    ctx.note('hold', signal='TEST', reason='ok')\n"
)


async def _create_native_strategy(client: AsyncClient, headers: dict, name: str = "Native API Strategy") -> str:
    resp = await client.post(
        "/api/v1/strategies",
        json={"name": name, "version": {"python_code": TRIVIAL_NATIVE_CODE, "is_native": True}},
        headers=headers,
    )
    assert resp.status_code == 201, resp.text
    assert resp.json()["code_type"] == "native"
    return resp.json()["id"]


async def test_full_native_paper_trading_flow_via_api(client: AsyncClient, seeded_admin: dict, db_session: AsyncSession):
    token = await _login(client, seeded_admin["email"], seeded_admin["password"])
    headers = {"Authorization": f"Bearer {token}"}
    portfolio_id = await _default_portfolio_id(client, headers)
    strategy_id = await _create_native_strategy(client, headers)

    deploy_resp = await client.post(
        "/api/v1/paper-trading/native-deployments",
        json={"strategy_id": strategy_id, "portfolio_id": portfolio_id},
        headers=headers,
    )
    assert deploy_resp.status_code == 201, deploy_resp.text
    deployment_id = deploy_resp.json()["id"]
    assert deploy_resp.json()["status"] == "active"
    assert deploy_resp.json()["portfolio_id"] == portfolio_id

    eval_resp = await client.post(f"/api/v1/paper-trading/native-deployments/{deployment_id}/evaluate", headers=headers)
    assert eval_resp.status_code == 200
    assert eval_resp.json()["action"] == "hold"
    assert eval_resp.json()["signal"] == "TEST"

    list_resp = await client.get("/api/v1/paper-trading/native-deployments", headers=headers)
    assert any(d["id"] == deployment_id for d in list_resp.json())

    strategy_after = await client.get(f"/api/v1/strategies/{strategy_id}", headers=headers)
    assert strategy_after.json()["status"] == "paper_trading"

    stop_resp = await client.post(f"/api/v1/paper-trading/native-deployments/{deployment_id}/stop", headers=headers)
    assert stop_resp.status_code == 200
    assert stop_resp.json()["status"] == "stopped"

    delete_resp = await client.delete(f"/api/v1/paper-trading/native-deployments/{deployment_id}", headers=headers)
    assert delete_resp.status_code == 204

    list_after = await client.get("/api/v1/paper-trading/native-deployments", headers=headers)
    assert not any(d["id"] == deployment_id for d in list_after.json())


async def test_native_deployment_rejects_non_native_strategy(client: AsyncClient, seeded_admin: dict, db_session: AsyncSession):
    token = await _login(client, seeded_admin["email"], seeded_admin["password"])
    headers = {"Authorization": f"Bearer {token}"}
    portfolio_id = await _default_portfolio_id(client, headers)

    strategy_resp = await client.post(
        "/api/v1/strategies",
        json={"name": "Plain Python Strategy", "version": {"python_code": "def generate_signal(candles, params):\n    return 'HOLD'\n"}},
        headers=headers,
    )
    strategy_id = strategy_resp.json()["id"]
    assert strategy_resp.json()["code_type"] == "python"

    resp = await client.post(
        "/api/v1/paper-trading/native-deployments",
        json={"strategy_id": strategy_id, "portfolio_id": portfolio_id},
        headers=headers,
    )
    assert resp.status_code == 400


async def test_native_strategy_validate_checks_for_evaluate_function(client: AsyncClient, seeded_admin: dict, db_session: AsyncSession):
    token = await _login(client, seeded_admin["email"], seeded_admin["password"])
    headers = {"Authorization": f"Bearer {token}"}
    strategy_id = await _create_native_strategy(client, headers, name="Validate Native OK")

    resp = await client.post(f"/api/v1/strategies/{strategy_id}/validate", headers=headers)
    assert resp.status_code == 200
    assert resp.json()["valid"] is True

    bad_resp = await client.post(
        "/api/v1/strategies",
        json={"name": "Validate Native Bad", "version": {"python_code": "x = 1\n", "is_native": True}},
        headers=headers,
    )
    bad_strategy_id = bad_resp.json()["id"]
    validate_bad = await client.post(f"/api/v1/strategies/{bad_strategy_id}/validate", headers=headers)
    assert validate_bad.json()["valid"] is False
    assert "evaluate" in validate_bad.json()["error"]


async def test_native_deployment_out_computes_spread_position(client: AsyncClient, seeded_admin: dict, db_session: AsyncSession):
    """Regression test for the Paper Trading page's Advanced Strategy
    Deployments table: given a deployment whose state has the short/long
    credit-spread shape, the API response's `position` must reconstruct
    trade_value (net credit at entry) and live_value/unrealized_pnl from
    current tick prices -- the frontend has no direct TickEngine access,
    so this has to happen server-side."""
    from datetime import datetime, timezone

    from app.models.instrument import Instrument
    from app.services.market_data.tick_engine import tick_engine

    token = await _login(client, seeded_admin["email"], seeded_admin["password"])
    headers = {"Authorization": f"Bearer {token}"}
    portfolio_id = await _default_portfolio_id(client, headers)

    short_inst = Instrument(
        exchange="NFO", symbol="NIFTY25SEP23300PE", name="Nifty 23300 PE", instrument_type="option",
        data_source="zerodha_kite", external_ref="NIFTY25SEP23300PE", strike=23300.0, option_type="PE", lot_size=65,
    )
    long_inst = Instrument(
        exchange="NFO", symbol="NIFTY25SEP23100PE", name="Nifty 23100 PE", instrument_type="option",
        data_source="zerodha_kite", external_ref="NIFTY25SEP23100PE", strike=23100.0, option_type="PE", lot_size=65,
    )
    db_session.add_all([short_inst, long_inst])
    await db_session.commit()
    tick_engine.set_real_price(short_inst.id, 100.0, "test")  # cheaper now than the 112.5 entry -- a winning spread
    tick_engine.set_real_price(long_inst.id, 60.0, "test")

    code = (
        "async def evaluate(ctx):\n"
        "    ctx.note('hold', reason='position seeded directly for this test')\n"
    )
    strategy_resp = await client.post(
        "/api/v1/strategies",
        json={"name": "Position Display Test", "version": {"python_code": code, "is_native": True}},
        headers=headers,
    )
    strategy_id = strategy_resp.json()["id"]
    deploy_resp = await client.post(
        "/api/v1/paper-trading/native-deployments",
        json={"strategy_id": strategy_id, "portfolio_id": portfolio_id},
        headers=headers,
    )
    deployment_id = deploy_resp.json()["id"]

    import uuid as uuid_mod

    from app.models.paper_trading import PaperNativeDeployment

    deployment = await db_session.get(PaperNativeDeployment, uuid_mod.UUID(deployment_id))
    deployment.state = {
        "position": {
            "bias": "bullish", "pcr_at_entry": 1.4, "expiry": "2026-09-25",
            "short": {"instrument_id": str(short_inst.id), "strike": 23300.0, "quantity": 130.0, "entry_price": 112.5},
            "long": {"instrument_id": str(long_inst.id), "strike": 23100.0, "quantity": 130.0, "entry_price": 64.95},
            "opened_at": datetime.now(timezone.utc).isoformat(),
        }
    }
    await db_session.commit()

    list_resp = await client.get("/api/v1/paper-trading/native-deployments", headers=headers)
    deployment_out = next(d for d in list_resp.json() if d["id"] == deployment_id)
    position = deployment_out["position"]
    assert position is not None
    assert position["bias"] == "bullish"
    assert len(position["legs"]) == 2
    trade_value = (112.5 - 64.95) * 130.0
    live_value = (100.0 - 60.0) * 130.0
    # approx: the endpoint sums per-leg products, which rounds differently
    # from this one-shot expression in the last float digit.
    assert position["trade_value"] == pytest.approx(trade_value)
    assert position["live_value"] == pytest.approx(live_value)
    assert position["unrealized_pnl"] == pytest.approx(trade_value - live_value)
    assert position["metrics"] == {"pcr_at_entry": 1.4, "expiry": "2026-09-25"}  # bias/legs/opened_at excluded


async def test_native_deployment_out_computes_multi_leg_position(client: AsyncClient, seeded_admin: dict, db_session: AsyncSession):
    """Regression test: nifty_pcr_multi_regime's iron condor (4 named legs
    under state["position"]["legs"], each with its own explicit "sell"/
    "buy" side) must reconstruct the same way the 2-leg "short"/"long"
    shape above does -- before this fix, _build_position_out only ever
    recognized the 2-leg shape, so a deployment running this strategy
    always showed "flat" on the dashboard even while genuinely holding an
    open position."""
    from datetime import datetime, timezone

    from app.models.instrument import Instrument
    from app.services.market_data.tick_engine import tick_engine

    token = await _login(client, seeded_admin["email"], seeded_admin["password"])
    headers = {"Authorization": f"Bearer {token}"}
    portfolio_id = await _default_portfolio_id(client, headers)

    nifty = Instrument(
        exchange="NSE", symbol="NIFTY 50", name="Nifty 50", instrument_type="index", data_source="zerodha_kite", external_ref="NIFTY 50",
    )
    db_session.add(nifty)
    await db_session.flush()
    short_ce = Instrument(
        exchange="NFO", symbol="NIFTY25SEP23400CE", name="Nifty 23400 CE", instrument_type="option",
        data_source="zerodha_kite", external_ref="NIFTY25SEP23400CE", strike=23400.0, option_type="CE", lot_size=65,
        underlying_instrument_id=nifty.id,
    )
    short_pe = Instrument(
        exchange="NFO", symbol="NIFTY25SEP23400PE", name="Nifty 23400 PE", instrument_type="option",
        data_source="zerodha_kite", external_ref="NIFTY25SEP23400PE", strike=23400.0, option_type="PE", lot_size=65,
    )
    long_ce = Instrument(
        exchange="NFO", symbol="NIFTY25SEP23600CE", name="Nifty 23600 CE", instrument_type="option",
        data_source="zerodha_kite", external_ref="NIFTY25SEP23600CE", strike=23600.0, option_type="CE", lot_size=65,
    )
    long_pe = Instrument(
        exchange="NFO", symbol="NIFTY25SEP23200PE", name="Nifty 23200 PE", instrument_type="option",
        data_source="zerodha_kite", external_ref="NIFTY25SEP23200PE", strike=23200.0, option_type="PE", lot_size=65,
    )
    db_session.add_all([short_ce, short_pe, long_ce, long_pe])
    await db_session.commit()
    tick_engine.set_real_price(short_ce.id, 100.0, "test")
    tick_engine.set_real_price(short_pe.id, 90.0, "test")
    tick_engine.set_real_price(long_ce.id, 30.0, "test")
    tick_engine.set_real_price(long_pe.id, 25.0, "test")
    tick_engine.set_real_price(nifty.id, 23416.6, "test")

    code = "async def evaluate(ctx):\n    ctx.note('hold', reason='position seeded directly for this test')\n"
    strategy_resp = await client.post(
        "/api/v1/strategies",
        json={"name": "Multi-Regime Position Display Test", "version": {"python_code": code, "is_native": True}},
        headers=headers,
    )
    strategy_id = strategy_resp.json()["id"]
    deploy_resp = await client.post(
        "/api/v1/paper-trading/native-deployments",
        json={"strategy_id": strategy_id, "portfolio_id": portfolio_id},
        headers=headers,
    )
    deployment_id = deploy_resp.json()["id"]

    import uuid as uuid_mod

    from app.models.paper_trading import PaperNativeDeployment

    deployment = await db_session.get(PaperNativeDeployment, uuid_mod.UUID(deployment_id))
    deployment.state = {
        "position": {
            "regime": "sideways", "pcr_at_entry": 0.99, "entry_spot": 23377.4, "expiry": "2026-09-25",
            "legs": {
                "short_ce": {"instrument_id": str(short_ce.id), "strike": 23400.0, "option_type": "CE", "side": "sell", "quantity": 650.0, "entry_price": 117.5},
                "short_pe": {"instrument_id": str(short_pe.id), "strike": 23400.0, "option_type": "PE", "side": "sell", "quantity": 650.0, "entry_price": 64.45},
                "long_ce": {"instrument_id": str(long_ce.id), "strike": 23600.0, "option_type": "CE", "side": "buy", "quantity": 650.0, "entry_price": 30.05},
                "long_pe": {"instrument_id": str(long_pe.id), "strike": 23200.0, "option_type": "PE", "side": "buy", "quantity": 650.0, "entry_price": 21.20},
            },
            "opened_at": datetime.now(timezone.utc).isoformat(),
        }
    }
    await db_session.commit()

    list_resp = await client.get("/api/v1/paper-trading/native-deployments", headers=headers)
    deployment_out = next(d for d in list_resp.json() if d["id"] == deployment_id)
    position = deployment_out["position"]
    assert position is not None, "a 4-leg iron condor position must not display as flat"
    assert position["bias"] == "sideways"
    assert len(position["legs"]) == 4
    assert {leg["side"] for leg in position["legs"]} == {"short", "long"}

    trade_value = (117.5 + 64.45 - 30.05 - 21.20) * 650.0
    live_value = (100.0 + 90.0 - 30.0 - 25.0) * 650.0
    # approx: the endpoint sums per-leg products, which rounds differently
    # from this one-shot expression in the last float digit.
    assert position["trade_value"] == pytest.approx(trade_value)
    assert position["live_value"] == pytest.approx(live_value)
    assert position["unrealized_pnl"] == pytest.approx(trade_value - live_value)
    # The strategy's own position fields pass through (not its legs/regime,
    # shown elsewhere), plus the legs' underlying and its live price.
    assert position["metrics"] == {"pcr_at_entry": 0.99, "entry_spot": 23377.4, "expiry": "2026-09-25"}
    assert position["underlying_symbol"] == "NIFTY 50"
    assert position["underlying_price"] == 23416.6


async def test_native_deployment_out_computes_holdings(client: AsyncClient, seeded_admin: dict, db_session: AsyncSession):
    """Regression test: a multi-holding strategy (e.g. the MACD/RSI
    rotation strategy, which buys several independent stocks under
    state["holdings"] rather than one spread under state["position"])
    must show up in the API response's `holdings` field -- before this
    fix, a deployment holding real stocks always showed nothing at all
    on the dashboard, since only state["position"] was ever read."""
    from datetime import datetime, timezone

    from app.models.instrument import Instrument
    from app.services.market_data.tick_engine import tick_engine

    token = await _login(client, seeded_admin["email"], seeded_admin["password"])
    headers = {"Authorization": f"Bearer {token}"}
    portfolio_id = await _default_portfolio_id(client, headers)

    stock_a = Instrument(
        exchange="NSE", symbol="LAURUSLABS", name="Laurus Labs", instrument_type="equity",
        data_source="zerodha_kite", external_ref="LAURUSLABS",
    )
    stock_b = Instrument(
        exchange="NSE", symbol="POLYCAB", name="Polycab India", instrument_type="equity",
        data_source="zerodha_kite", external_ref="POLYCAB",
    )
    db_session.add_all([stock_a, stock_b])
    await db_session.commit()
    tick_engine.set_real_price(stock_a.id, 550.0, "test")
    tick_engine.set_real_price(stock_b.id, 6200.0, "test")

    code = "async def evaluate(ctx):\n    ctx.note('hold', reason='holdings seeded directly for this test')\n"
    strategy_resp = await client.post(
        "/api/v1/strategies",
        json={"name": "Holdings Display Test", "version": {"python_code": code, "is_native": True}},
        headers=headers,
    )
    strategy_id = strategy_resp.json()["id"]
    deploy_resp = await client.post(
        "/api/v1/paper-trading/native-deployments",
        json={"strategy_id": strategy_id, "portfolio_id": portfolio_id},
        headers=headers,
    )
    deployment_id = deploy_resp.json()["id"]

    import uuid as uuid_mod

    from app.models.paper_trading import PaperNativeDeployment

    deployment = await db_session.get(PaperNativeDeployment, uuid_mod.UUID(deployment_id))
    deployment.state = {
        "seeded": True,
        "holdings": {
            "LAURUSLABS": {
                "instrument_id": str(stock_a.id), "quantity": 16.0, "entry_price": 500.0,
                "opened_at": "2026-09-21T09:45:00+05:30",
                # Strategy-specific per-holding values -- passed through as metrics.
                "rank": 2, "rsi": 68.2, "macd": 1.35, "signal": "Above 0", "note_list": ["not", "a", "scalar"],
            },
            "POLYCAB": {"instrument_id": str(stock_b.id), "quantity": 1.0, "entry_price": 6000.0, "opened_at": datetime.now(timezone.utc).isoformat()},
        },
    }
    await db_session.commit()

    list_resp = await client.get("/api/v1/paper-trading/native-deployments", headers=headers)
    deployment_out = next(d for d in list_resp.json() if d["id"] == deployment_id)
    assert deployment_out["position"] is None
    holdings = deployment_out["holdings"]
    assert holdings is not None
    assert {h["instrument_symbol"] for h in holdings} == {"LAURUSLABS", "POLYCAB"}
    assert all(h["side"] == "long" for h in holdings)
    laurus = next(h for h in holdings if h["instrument_symbol"] == "LAURUSLABS")
    assert laurus["entry_price"] == 500.0
    assert laurus["current_price"] == 550.0
    assert laurus["opened_at"].startswith("2026-09-21T")
    # Every extra scalar the strategy stored comes through; non-scalars and
    # the core leg keys don't.
    assert laurus["metrics"] == {"rank": 2, "rsi": 68.2, "macd": 1.35, "signal": "Above 0"}
    polycab = next(h for h in holdings if h["instrument_symbol"] == "POLYCAB")
    assert polycab["metrics"] == {}


async def test_native_trades_endpoint_lists_closed_trades(client: AsyncClient, seeded_admin: dict, db_session: AsyncSession):
    token = await _login(client, seeded_admin["email"], seeded_admin["password"])
    headers = {"Authorization": f"Bearer {token}"}
    portfolio_id = await _default_portfolio_id(client, headers)

    recording_code = (
        "from datetime import datetime, timezone\n"
        "async def evaluate(ctx):\n"
        "    await ctx.record_trade(legs=[], pnl=42.0, pnl_pct=1.0, exit_reason='manual', opened_at=datetime.now(timezone.utc))\n"
        "    ctx.note('exited', signal='COVER')\n"
    )
    strategy_resp = await client.post(
        "/api/v1/strategies",
        json={"name": "Recording Native Strategy", "version": {"python_code": recording_code, "is_native": True}},
        headers=headers,
    )
    strategy_id = strategy_resp.json()["id"]

    deploy_resp = await client.post(
        "/api/v1/paper-trading/native-deployments",
        json={"strategy_id": strategy_id, "portfolio_id": portfolio_id},
        headers=headers,
    )
    deployment_id = deploy_resp.json()["id"]

    await client.post(f"/api/v1/paper-trading/native-deployments/{deployment_id}/evaluate", headers=headers)

    trades_resp = await client.get(f"/api/v1/paper-trading/native-trades?deployment_id={deployment_id}", headers=headers)
    assert trades_resp.status_code == 200
    trades = trades_resp.json()
    assert len(trades) == 1
    assert trades[0]["pnl"] == 42.0
    assert trades[0]["exit_reason"] == "manual"

    all_trades_resp = await client.get("/api/v1/paper-trading/native-trades", headers=headers)
    assert any(t["deployment_id"] == deployment_id for t in all_trades_resp.json())


async def test_native_trades_endpoint_enriches_legs_with_instrument_symbol(client: AsyncClient, seeded_admin: dict, db_session: AsyncSession):
    """Regression test: a trade's legs are recorded with only an
    instrument_id (see NativeContext.record_trade's docstring), so the
    Closed Trades table used to render "long 16@11896.00->11145.00" with
    no way to tell which instrument that was. The API must resolve each
    leg's instrument_id to its symbol before returning it."""
    from app.models.instrument import Instrument

    token = await _login(client, seeded_admin["email"], seeded_admin["password"])
    headers = {"Authorization": f"Bearer {token}"}
    portfolio_id = await _default_portfolio_id(client, headers)

    instrument = Instrument(
        exchange="NSE", symbol="CENTURYTEX", name="Century Textiles", instrument_type="equity",
        data_source="zerodha_kite", external_ref="CENTURYTEX",
    )
    db_session.add(instrument)
    await db_session.commit()
    instrument_id = str(instrument.id)

    recording_code = (
        "from datetime import datetime, timezone\n"
        "async def evaluate(ctx):\n"
        "    await ctx.record_trade(\n"
        "        legs=[{'instrument_id': " + repr(instrument_id) + ", 'side': 'long', 'quantity': 16, 'entry_price': 11896.0, 'exit_price': 11145.0}],\n"
        "        pnl=-12016.0, pnl_pct=-6.3, exit_reason='macd_signal_zero_cross_down', opened_at=datetime.now(timezone.utc),\n"
        "    )\n"
        "    ctx.note('exited', signal='COVER')\n"
    )
    strategy_resp = await client.post(
        "/api/v1/strategies",
        json={"name": "Leg Symbol Test Strategy", "version": {"python_code": recording_code, "is_native": True}},
        headers=headers,
    )
    strategy_id = strategy_resp.json()["id"]

    deploy_resp = await client.post(
        "/api/v1/paper-trading/native-deployments",
        json={"strategy_id": strategy_id, "portfolio_id": portfolio_id},
        headers=headers,
    )
    deployment_id = deploy_resp.json()["id"]

    await client.post(f"/api/v1/paper-trading/native-deployments/{deployment_id}/evaluate", headers=headers)

    trades_resp = await client.get(f"/api/v1/paper-trading/native-trades?deployment_id={deployment_id}", headers=headers)
    leg = trades_resp.json()[0]["legs"][0]
    assert leg["instrument_symbol"] == "CENTURYTEX"
    assert leg["instrument_id"] == instrument_id


async def test_deployment_switches_to_latest_saved_version_keeping_its_state(client: AsyncClient, seeded_admin: dict):
    """Saving new code adds a strategy version; a running deployment keeps
    the version it was started with (Restart included) until switched --
    and switching keeps its holdings, so the new code carries on from them."""
    token = await _login(client, seeded_admin["email"], seeded_admin["password"])
    headers = {"Authorization": f"Bearer {token}"}
    portfolio_id = await _default_portfolio_id(client, headers)
    v1_code = (
        "async def evaluate(ctx):\n"
        "    ctx.state.setdefault('basket', ['SOLARINDS'])\n"
        "    ctx.note('hold', signal='V1')\n"
    )
    v2_code = (
        "async def evaluate(ctx):\n"
        "    ctx.note('hold', signal='V2', reason='holding ' + ','.join(ctx.state['basket']))\n"
    )
    strategy_id = (await client.post(
        "/api/v1/strategies", json={"name": "Versioned Native", "version": {"python_code": v1_code, "is_native": True}}, headers=headers,
    )).json()["id"]
    deployment_id = (await client.post(
        "/api/v1/paper-trading/native-deployments", json={"strategy_id": strategy_id, "portfolio_id": portfolio_id}, headers=headers,
    )).json()["id"]
    await client.post(f"/api/v1/paper-trading/native-deployments/{deployment_id}/evaluate", headers=headers)

    save = await client.post(f"/api/v1/strategies/{strategy_id}/versions", json={"python_code": v2_code, "is_native": True}, headers=headers)
    assert save.status_code == 200, save.text

    deployment = next(d for d in (await client.get("/api/v1/paper-trading/native-deployments", headers=headers)).json() if d["id"] == deployment_id)
    assert (deployment["version_number"], deployment["latest_version_number"]) == (1, 2)
    evaluated = await client.post(f"/api/v1/paper-trading/native-deployments/{deployment_id}/evaluate", headers=headers)
    assert evaluated.json()["signal"] == "V1"  # saving alone changes nothing that's running

    switched = await client.post(f"/api/v1/paper-trading/native-deployments/{deployment_id}/use-latest-version", headers=headers)
    assert switched.status_code == 200, switched.text
    assert switched.json()["version_number"] == 2
    evaluated = await client.post(f"/api/v1/paper-trading/native-deployments/{deployment_id}/evaluate", headers=headers)
    assert evaluated.json()["signal"] == "V2"
    assert evaluated.json()["reason"] == "holding SOLARINDS"  # v1's state carried over

    again = await client.post(f"/api/v1/paper-trading/native-deployments/{deployment_id}/use-latest-version", headers=headers)
    assert again.status_code == 409
