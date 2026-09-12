import uuid
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, Request, status
from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.core.deps import require_role
from app.core.security import hash_password
from app.db.session import get_db
from app.models.broker import BrokerAccount
from app.models.live_trading import LiveDeployment
from app.models.paper_trading import PaperPortfolio
from app.models.session import Session as SessionModel
from app.models.strategy import Strategy
from app.models.user import Role, User, UserRole
from app.schemas.user import AdminResetPassword, UserApprove, UserCreate, UserOut, UserUpdate, UserUpdateRoles
from app.services.audit import write_audit_log

router = APIRouter()


def _to_out(user: User) -> UserOut:
    return UserOut(
        id=str(user.id), email=user.email, full_name=user.full_name, is_active=user.is_active,
        is_approved=user.is_approved, password_reset_requested=user.password_reset_requested,
        roles=user.role_names,
    )


@router.get("", response_model=list[UserOut])
async def list_users(
    db: AsyncSession = Depends(get_db), admin: User = Depends(require_role("administrator"))
) -> list[UserOut]:
    result = await db.execute(select(User).options(selectinload(User.user_roles).selectinload(UserRole.role)))
    return [_to_out(u) for u in result.scalars().all()]


@router.post("", response_model=UserOut, status_code=status.HTTP_201_CREATED)
async def create_user(
    payload: UserCreate,
    request: Request,
    db: AsyncSession = Depends(get_db),
    admin: User = Depends(require_role("administrator")),
) -> UserOut:
    existing = await db.execute(select(User).where(User.email == payload.email))
    if existing.scalar_one_or_none() is not None:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Email already registered")

    roles_result = await db.execute(select(Role).where(Role.name.in_(payload.roles)))
    roles = roles_result.scalars().all()
    if len(roles) != len(set(payload.roles)):
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="One or more roles do not exist")

    user = User(email=payload.email, hashed_password=hash_password(payload.password), full_name=payload.full_name)
    user.user_roles = [UserRole(role=role) for role in roles]
    db.add(user)
    await db.flush()

    await write_audit_log(
        db,
        user_id=admin.id,
        action="USER_CREATED",
        object_type="user",
        object_id=str(user.id),
        new_value={"email": user.email, "roles": payload.roles},
        ip_address=request.client.host if request.client else None,
    )
    await db.commit()
    await db.refresh(user, attribute_names=["user_roles"])
    return _to_out(user)


@router.post("/{user_id}/approve", response_model=UserOut)
async def approve_user(
    user_id: str,
    payload: UserApprove,
    request: Request,
    db: AsyncSession = Depends(get_db),
    admin: User = Depends(require_role("administrator")),
) -> UserOut:
    result = await db.execute(
        select(User)
        .options(selectinload(User.user_roles).selectinload(UserRole.role))
        .where(User.id == uuid.UUID(user_id))
    )
    user = result.scalar_one_or_none()
    if user is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="User not found")
    if user.is_approved:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="User is already approved")

    roles_result = await db.execute(select(Role).where(Role.name.in_(payload.roles)))
    roles = roles_result.scalars().all()
    if len(roles) != len(set(payload.roles)):
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="One or more roles do not exist")

    user.is_approved = True
    user.user_roles = [UserRole(role=role) for role in roles]
    await db.flush()

    await write_audit_log(
        db,
        user_id=admin.id,
        action="USER_APPROVED",
        object_type="user",
        object_id=str(user.id),
        new_value={"email": user.email, "roles": payload.roles},
        ip_address=request.client.host if request.client else None,
    )
    await db.commit()
    await db.refresh(user, attribute_names=["user_roles"])
    return _to_out(user)


@router.post("/{user_id}/reject", status_code=status.HTTP_204_NO_CONTENT)
async def reject_user(
    user_id: str,
    request: Request,
    db: AsyncSession = Depends(get_db),
    admin: User = Depends(require_role("administrator")),
) -> None:
    result = await db.execute(select(User).where(User.id == uuid.UUID(user_id)))
    user = result.scalar_one_or_none()
    if user is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="User not found")
    if user.is_approved:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail="Cannot reject an already-approved user"
        )

    await write_audit_log(
        db,
        user_id=admin.id,
        action="USER_REJECTED",
        object_type="user",
        object_id=str(user.id),
        previous_value={"email": user.email},
        ip_address=request.client.host if request.client else None,
    )
    await db.delete(user)
    await db.commit()


@router.post("/{user_id}/reset-password", response_model=UserOut)
async def reset_password(
    user_id: str,
    payload: AdminResetPassword,
    request: Request,
    db: AsyncSession = Depends(get_db),
    admin: User = Depends(require_role("administrator")),
) -> UserOut:
    result = await db.execute(
        select(User).options(selectinload(User.user_roles).selectinload(UserRole.role)).where(User.id == uuid.UUID(user_id))
    )
    user = result.scalar_one_or_none()
    if user is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="User not found")

    user.hashed_password = hash_password(payload.new_password)
    user.password_reset_requested = False
    # Force re-login everywhere with the new password.
    await db.execute(
        update(SessionModel)
        .where(SessionModel.user_id == user.id, SessionModel.revoked_at.is_(None))
        .values(revoked_at=datetime.now(timezone.utc))
    )

    await write_audit_log(
        db,
        user_id=admin.id,
        action="PASSWORD_RESET_BY_ADMIN",
        object_type="user",
        object_id=str(user.id),
        ip_address=request.client.host if request.client else None,
    )
    await db.commit()
    await db.refresh(user, attribute_names=["user_roles"])
    return _to_out(user)


async def _active_administrator_count(db: AsyncSession, exclude_user_id: uuid.UUID | None = None) -> int:
    stmt = (
        select(func.count(func.distinct(User.id)))
        .join(UserRole, UserRole.user_id == User.id)
        .join(Role, Role.id == UserRole.role_id)
        .where(Role.name == "administrator", User.is_active.is_(True))
    )
    if exclude_user_id is not None:
        stmt = stmt.where(User.id != exclude_user_id)
    result = await db.execute(stmt)
    return result.scalar_one()


@router.patch("/{user_id}", response_model=UserOut)
async def update_user(
    user_id: str,
    payload: UserUpdate,
    request: Request,
    db: AsyncSession = Depends(get_db),
    admin: User = Depends(require_role("administrator")),
) -> UserOut:
    """Edits name/roles/active-status in one call -- PUT /{user_id}/roles
    above still exists unchanged for its own callers, this is the general
    "Edit User" form's single save action."""
    result = await db.execute(
        select(User).options(selectinload(User.user_roles).selectinload(UserRole.role)).where(User.id == uuid.UUID(user_id))
    )
    user = result.scalar_one_or_none()
    if user is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="User not found")

    is_self = user.id == admin.id
    previous = {"full_name": user.full_name, "roles": user.role_names, "is_active": user.is_active}

    if payload.roles is not None:
        roles_result = await db.execute(select(Role).where(Role.name.in_(payload.roles)))
        roles = roles_result.scalars().all()
        if len(roles) != len(set(payload.roles)):
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="One or more roles do not exist")
        if is_self and "administrator" not in payload.roles:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="You cannot remove your own administrator role")
        if "administrator" not in payload.roles and await _active_administrator_count(db, exclude_user_id=user.id) == 0:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Cannot remove the last administrator's admin role")
        user.user_roles = [UserRole(role=role) for role in roles]

    if payload.is_active is not None:
        if payload.is_active is False and is_self:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="You cannot deactivate your own account")
        if payload.is_active is False and "administrator" in user.role_names and await _active_administrator_count(db, exclude_user_id=user.id) == 0:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Cannot deactivate the last administrator")
        user.is_active = payload.is_active
        if payload.is_active is False:
            await db.execute(
                update(SessionModel)
                .where(SessionModel.user_id == user.id, SessionModel.revoked_at.is_(None))
                .values(revoked_at=datetime.now(timezone.utc))
            )

    if payload.full_name is not None:
        user.full_name = payload.full_name

    await db.flush()
    await write_audit_log(
        db, user_id=admin.id, action="USER_UPDATED", object_type="user", object_id=str(user.id),
        previous_value=previous, new_value={"full_name": user.full_name, "roles": user.role_names, "is_active": user.is_active},
        ip_address=request.client.host if request.client else None,
    )
    await db.commit()
    await db.refresh(user, attribute_names=["user_roles"])
    return _to_out(user)


@router.delete("/{user_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_user(
    user_id: str,
    request: Request,
    db: AsyncSession = Depends(get_db),
    admin: User = Depends(require_role("administrator")),
) -> None:
    """Hard delete -- refuses if the account owns any real data (a
    strategy, broker account, paper portfolio, or live deployment),
    mirroring strategies.py's identical guard for the same reason:
    every one of those cascades on delete (ondelete="CASCADE" on
    owner_id/user_id), so deleting an active trader would silently wipe
    their whole trading history. Deactivate (PATCH is_active=false)
    instead of deleting an account with real history."""
    result = await db.execute(
        select(User).options(selectinload(User.user_roles).selectinload(UserRole.role)).where(User.id == uuid.UUID(user_id))
    )
    user = result.scalar_one_or_none()
    if user is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="User not found")
    if user.id == admin.id:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="You cannot delete your own account")
    if "administrator" in user.role_names and await _active_administrator_count(db, exclude_user_id=user.id) == 0:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Cannot delete the last administrator")

    owns_data = any([
        (await db.execute(select(Strategy.id).where(Strategy.owner_id == user.id).limit(1))).scalar_one_or_none(),
        (await db.execute(select(BrokerAccount.id).where(BrokerAccount.user_id == user.id).limit(1))).scalar_one_or_none(),
        (await db.execute(select(PaperPortfolio.id).where(PaperPortfolio.user_id == user.id).limit(1))).scalar_one_or_none(),
        (await db.execute(select(LiveDeployment.id).where(LiveDeployment.owner_id == user.id).limit(1))).scalar_one_or_none(),
    ])
    if owns_data:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="This account owns strategies, broker accounts, or trading history -- deleting would destroy real "
            "records. Deactivate the account instead (Edit > set Inactive) to block their login without losing data.",
        )

    await write_audit_log(
        db, user_id=admin.id, action="USER_DELETED", object_type="user", object_id=str(user.id),
        previous_value={"email": user.email, "full_name": user.full_name, "roles": user.role_names},
        ip_address=request.client.host if request.client else None,
    )
    await db.delete(user)
    await db.commit()


@router.put("/{user_id}/roles", response_model=UserOut)
async def update_user_roles(
    user_id: str,
    payload: UserUpdateRoles,
    request: Request,
    db: AsyncSession = Depends(get_db),
    admin: User = Depends(require_role("administrator")),
) -> UserOut:
    result = await db.execute(
        select(User)
        .options(selectinload(User.user_roles).selectinload(UserRole.role))
        .where(User.id == uuid.UUID(user_id))
    )
    user = result.scalar_one_or_none()
    if user is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="User not found")

    roles_result = await db.execute(select(Role).where(Role.name.in_(payload.roles)))
    roles = roles_result.scalars().all()
    if len(roles) != len(set(payload.roles)):
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="One or more roles do not exist")

    previous_roles = user.role_names
    user.user_roles = [UserRole(role=role) for role in roles]
    await db.flush()

    await write_audit_log(
        db,
        user_id=admin.id,
        action="USER_ROLES_UPDATED",
        object_type="user",
        object_id=str(user.id),
        previous_value={"roles": previous_roles},
        new_value={"roles": payload.roles},
        ip_address=request.client.host if request.client else None,
    )
    await db.commit()
    await db.refresh(user, attribute_names=["user_roles"])
    return _to_out(user)
