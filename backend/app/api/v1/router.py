from fastapi import APIRouter

from app.api.v1.endpoints import auth, brokers, system, users

api_router = APIRouter()
api_router.include_router(auth.router, prefix="/auth", tags=["auth"])
api_router.include_router(users.router, prefix="/users", tags=["users"])
api_router.include_router(brokers.router, prefix="/brokers", tags=["brokers"])
api_router.include_router(system.router, prefix="/system", tags=["system"])
