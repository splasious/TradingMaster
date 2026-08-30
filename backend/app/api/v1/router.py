from fastapi import APIRouter

from app.api.v1.endpoints import (
    alerts,
    auth,
    backfill_platform,
    backtests,
    backup,
    brokers,
    indicators,
    instruments,
    live_trading,
    market_data,
    market_data_ws,
    optimization,
    paper_trading,
    reports,
    scanner,
    strategies,
    system,
    users,
)

api_router = APIRouter()
api_router.include_router(auth.router, prefix="/auth", tags=["auth"])
api_router.include_router(users.router, prefix="/users", tags=["users"])
api_router.include_router(brokers.router, prefix="/brokers", tags=["brokers"])
api_router.include_router(system.router, prefix="/system", tags=["system"])
api_router.include_router(instruments.router, prefix="/instruments", tags=["instruments"])
api_router.include_router(market_data.router, prefix="/market-data", tags=["market-data"])
api_router.include_router(market_data_ws.router, prefix="/ws", tags=["market-data-ws"])
api_router.include_router(indicators.router, prefix="/indicators", tags=["indicators"])
api_router.include_router(scanner.router, prefix="/scanner", tags=["scanner"])
api_router.include_router(strategies.router, prefix="/strategies", tags=["strategies"])
api_router.include_router(backtests.router, prefix="/backtests", tags=["backtests"])
api_router.include_router(optimization.router, prefix="/optimization", tags=["optimization"])
api_router.include_router(paper_trading.router, prefix="/paper-trading", tags=["paper-trading"])
api_router.include_router(live_trading.router, prefix="/live-trading", tags=["live-trading"])
api_router.include_router(alerts.router, prefix="/alerts", tags=["alerts"])
api_router.include_router(reports.router, prefix="/reports", tags=["reports"])
api_router.include_router(backup.router, prefix="/backup", tags=["backup"])
api_router.include_router(backfill_platform.router, prefix="/backfill-platform", tags=["backfill-platform"])
