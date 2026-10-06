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
    hash_password,
    hash_refresh_token,
    verify_password,
)
from app.core.time import as_aware_utc
from app.db.session import get_db
from app.models.session import Session as SessionModel
from app.models.user import User, UserRole
from app.schemas.auth import LoginRequest, TokenResponse
from app.schemas.user import ForgotPasswordRequest, UserOut, UserRegister
from app.services.audit import write_audit_log

router = APIRouter()
settings = get_settings()

REFRESH_COOKIE_NAME = "refresh_token"

# How long a refresh token keeps working after it was renewed. Every renewal
# replaces the token, so without this a second tab renewing at the same
# moment, or a renewal whose answer never reached the browser (a reload
# mid-request, a slow server), presented a token replaced seconds before and
# signed the user out -- several times an hour on 6 Oct.
RENEWAL_GRACE = timedelta(minutes=5)
_RENEWED_WITHIN = timedelta(seconds=2)


async def _just_renewed(db: AsyncSession, session: SessionModel, now: datetime) -> bool:
    """`session` was revoked by a renewal (one that issued its replacement at
    the same moment -- logout, a password reset or deactivation issue none)
    no more than RENEWAL_GRACE ago."""
    revoked = as_aware_utc(session.revoked_at)
    if now - revoked > RENEWAL_GRACE:
        return False
    replacement = await db.scalar(
        select(SessionModel.id).where(
            SessionModel.user_id == session.user_id, SessionModel.id != session.id,
            SessionModel.created_at >= revoked - _RENEWED_WITHIN, SessionModel.created_at <= revoked + _RENEWED_WITHIN,
        ).limit(1)
    )
    return replacement is not None


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


@router.post("/register", response_model=UserOut, status_code=status.HTTP_201_CREATED)
async def register(payload: UserRegister, request: Request, db: AsyncSession = Depends(get_db)) -> UserOut:
    existing = await db.execute(select(User).where(User.email == payload.email))
    if existing.scalar_one_or_none() is not None:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Email already registered")

    user = User(
        email=payload.email,
        hashed_password=hash_password(payload.password),
        full_name=payload.full_name,
        is_approved=False,
    )
    db.add(user)
    await db.flush()

    await write_audit_log(
        db,
        user_id=user.id,
        action="USER_SIGNED_UP",
        object_type="user",
        object_id=str(user.id),
        new_value={"email": user.email},
        ip_address=request.client.host if request.client else None,
    )
    await db.commit()
    await db.refresh(user, attribute_names=["user_roles"])
    return UserOut(
        id=str(user.id), email=user.email, full_name=user.full_name, is_active=user.is_active,
        is_approved=user.is_approved, password_reset_requested=user.password_reset_requested, roles=user.role_names,
    )


@router.post("/forgot-password", status_code=status.HTTP_204_NO_CONTENT)
async def forgot_password(
    payload: ForgotPasswordRequest, request: Request, db: AsyncSession = Depends(get_db)
) -> None:
    result = await db.execute(select(User).where(User.email == payload.email))
    user = result.scalar_one_or_none()
    # Always respond 204 regardless of whether the email is registered --
    # otherwise this endpoint would let anyone enumerate real accounts.
    if user is not None:
        user.password_reset_requested = True
        await write_audit_log(
            db,
            user_id=user.id,
            action="PASSWORD_RESET_REQUESTED",
            object_type="user",
            object_id=str(user.id),
            ip_address=request.client.host if request.client else None,
        )
        await db.commit()


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

    if not user.is_approved:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN, detail="Your account is pending administrator approval"
        )

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
    if session is None or as_aware_utc(session.expires_at) < now:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Refresh token invalid or expired")
    if session.revoked_at is not None and not await _just_renewed(db, session, now):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Refresh token invalid or expired")

    user_result = await db.execute(
        select(User).options(selectinload(User.user_roles).selectinload(UserRole.role)).where(User.id == session.user_id)
    )
    user = user_result.scalar_one_or_none()
    if user is None or not user.is_active:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="User inactive")

    # Rotate: revoke the used refresh token and issue a new one (a token
    # still inside its grace window was revoked already: it gets a session
    # of its own beside the one that replaced it).
    if session.revoked_at is None:
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
    return UserOut(
        id=str(user.id), email=user.email, full_name=user.full_name, is_active=user.is_active,
        is_approved=user.is_approved, password_reset_requested=user.password_reset_requested, roles=user.role_names,
    )
