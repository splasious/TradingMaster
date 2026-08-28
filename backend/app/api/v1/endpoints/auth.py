from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, HTTPException, Request, Response, status
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.core.config import get_settings
from app.core.deps import get_current_user
from app.core.security import (
    create_access_token,
    generate_refresh_token,
    hash_refresh_token,
    verify_password,
)
from app.core.time import as_aware_utc
from app.db.session import get_db
from app.models.session import Session as SessionModel
from app.models.user import User, UserRole
from app.schemas.auth import LoginRequest, TokenResponse
from app.schemas.user import UserOut
from app.services.audit import write_audit_log

router = APIRouter()
settings = get_settings()

REFRESH_COOKIE_NAME = "refresh_token"


async def _issue_session(db: AsyncSession, response: Response, user: User, request: Request) -> None:
    raw_refresh_token = generate_refresh_token()
    now = datetime.now(timezone.utc)
    db.add(
        SessionModel(
            user_id=user.id,
            refresh_token_hash=hash_refresh_token(raw_refresh_token),
            user_agent=request.headers.get("user-agent"),
            ip_address=request.client.host if request.client else None,
            created_at=now,
            expires_at=now + timedelta(days=settings.refresh_token_expire_days),
        )
    )
    response.set_cookie(
        key=REFRESH_COOKIE_NAME,
        value=raw_refresh_token,
        httponly=True,
        secure=settings.environment != "development",
        samesite="lax",
        max_age=settings.refresh_token_expire_days * 24 * 3600,
        path="/api/v1/auth",
    )


@router.post("/login", response_model=TokenResponse)
async def login(
    payload: LoginRequest, request: Request, response: Response, db: AsyncSession = Depends(get_db)
) -> TokenResponse:
    result = await db.execute(
        select(User)
        .options(selectinload(User.user_roles).selectinload(UserRole.role))
        .where(User.email == payload.email)
    )
    user = result.scalar_one_or_none()

    if user is None or not user.is_active or not verify_password(payload.password, user.hashed_password):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid email or password")

    access_token = create_access_token(subject=str(user.id), roles=user.role_names)
    await _issue_session(db, response, user, request)
    await write_audit_log(
        db,
        user_id=user.id,
        action="LOGIN",
        object_type="user",
        object_id=str(user.id),
        ip_address=request.client.host if request.client else None,
    )
    await db.commit()

    return TokenResponse(access_token=access_token, expires_in=settings.access_token_expire_minutes * 60)


@router.post("/refresh", response_model=TokenResponse)
async def refresh(request: Request, response: Response, db: AsyncSession = Depends(get_db)) -> TokenResponse:
    raw_refresh_token = request.cookies.get(REFRESH_COOKIE_NAME)
    if not raw_refresh_token:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="No refresh token")

    token_hash = hash_refresh_token(raw_refresh_token)
    result = await db.execute(select(SessionModel).where(SessionModel.refresh_token_hash == token_hash))
    session = result.scalar_one_or_none()

    now = datetime.now(timezone.utc)
    if session is None or session.revoked_at is not None or as_aware_utc(session.expires_at) < now:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Refresh token invalid or expired")

    user_result = await db.execute(
        select(User).options(selectinload(User.user_roles).selectinload(UserRole.role)).where(User.id == session.user_id)
    )
    user = user_result.scalar_one_or_none()
    if user is None or not user.is_active:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="User inactive")

    # Rotate: revoke the used refresh token and issue a new one.
    session.revoked_at = now
    access_token = create_access_token(subject=str(user.id), roles=user.role_names)
    await _issue_session(db, response, user, request)
    await db.commit()

    return TokenResponse(access_token=access_token, expires_in=settings.access_token_expire_minutes * 60)


@router.post("/logout", status_code=status.HTTP_204_NO_CONTENT)
async def logout(request: Request, response: Response, db: AsyncSession = Depends(get_db)) -> None:
    raw_refresh_token = request.cookies.get(REFRESH_COOKIE_NAME)
    if raw_refresh_token:
        token_hash = hash_refresh_token(raw_refresh_token)
        await db.execute(
            update(SessionModel)
            .where(SessionModel.refresh_token_hash == token_hash, SessionModel.revoked_at.is_(None))
            .values(revoked_at=datetime.now(timezone.utc))
        )
        await db.commit()
    response.delete_cookie(REFRESH_COOKIE_NAME, path="/api/v1/auth")


@router.get("/me", response_model=UserOut)
async def me(user: User = Depends(get_current_user)) -> UserOut:
    return UserOut(id=str(user.id), email=user.email, full_name=user.full_name, is_active=user.is_active, roles=user.role_names)
