from fastapi import APIRouter

from app.api.v1.endpoints import (
    auth,
    brokers,
    indicators,
    instruments,
    market_data,
    market_data_ws,
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
